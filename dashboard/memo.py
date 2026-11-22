import copy
import threading
import time


class SharedMemo:
    """Short-lived, process-wide memo with single-flight per key. Failures are never kept."""

    def __init__(self, ttl=3, clock=time.monotonic, size=64):
        self.ttl, self.clock, self.size = ttl, clock, size
        self.lock = threading.Lock()
        self.values, self.flights = {}, {}

    def get(self, key, compute):
        with self.lock:
            cached = self.values.get(key)
            if cached and self.clock() < cached[0]:
                return copy.deepcopy(cached[1])
            flight = self.flights.get(key)
            leader = flight is None
            if leader:
                flight = self.flights[key] = {"done": threading.Event()}
        if not leader:
            flight["done"].wait()
            if "error" in flight:
                raise flight["error"]
            return copy.deepcopy(flight["value"])
        try:
            value = compute()
        except BaseException as error:
            flight["error"] = error
            raise
        else:
            flight["value"] = value
            with self.lock:
                now = self.clock()
                self.values = {k: v for k, v in self.values.items() if now < v[0]}
                if len(self.values) < self.size:
                    self.values[key] = (now + self.ttl, value)
            return copy.deepcopy(value)
        finally:
            with self.lock:
                self.flights.pop(key, None)
            flight["done"].set()


class AzureMemo:
    """Shares regional Azure observations across users for a few seconds. Display reads only."""

    def __init__(self, azure, ttl=3, clock=time.monotonic):
        self.azure, self.memo = azure, SharedMemo(ttl, clock)

    def region(self, target, run_id, detail=False, baseline=None):
        key = (target.resource_id, run_id, bool(detail),
               tuple(sorted(baseline.items())) if baseline else None)
        return self.memo.get(key, lambda: self.azure.region(target, run_id, detail, baseline))
