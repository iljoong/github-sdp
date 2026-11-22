import threading
import time
from collections import deque
from datetime import datetime, timezone

from azure.core.exceptions import AzureError


def change_key(seconds):
    return "chg:" + datetime.fromtimestamp(seconds, timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


class ChangeFeed:
    """Follows the receiver's change hints in the inbox (hints only: they never authorize anything).

    Each newly seen hint expires the cached GitHub reads of its run and is numbered so every open
    event stream can ask for the runs announced since its own position. Rows are re-read over an
    `overlap` window so hints stamped by a receiver with a slightly different clock are not missed.
    """

    def __init__(self, inbox, invalidate, clock=time.time, interval=2.0, overlap=30.0, history=256):
        self.inbox, self.invalidate, self.clock = inbox, invalidate, clock
        self.interval, self.overlap = interval, overlap
        self.lock = threading.Lock()
        self.checked_at = float("-inf")
        self.sequence, self.events, self.seen = 0, deque(maxlen=history), set()
        self.cursor, self.floor = None, ""

    def poll(self):
        """Read new hints when the last read is older than `interval`; return the current sequence."""
        with self.lock:
            now = self.clock()
            if now - self.checked_at < self.interval:
                return self.sequence
            self.checked_at = now
        low = change_key(now - self.overlap)
        try:
            rows = self.inbox.changes(after=low)
        except AzureError:
            return self.sequence
        fresh = []
        with self.lock:
            self.floor = max(self.floor, low)
            self.seen = {key for key in self.seen if key > self.floor}
            for row in rows:
                if row["RowKey"] in self.seen or row["RowKey"] <= self.floor:
                    continue
                self.seen.add(row["RowKey"])
                self.sequence += 1
                self.events.append((self.sequence, row["run_id"]))
                fresh.append(row["run_id"])
                if self.cursor is None or row["RowKey"] > self.cursor:
                    self.cursor = row["RowKey"]
            sequence = self.sequence
        for run_id in dict.fromkeys(fresh):
            self.invalidate(run_id)
        return sequence

    def since(self, sequence):
        """Return (latest sequence, run IDs announced after `sequence`, each once, oldest first)."""
        with self.lock:
            runs = [run_id for number, run_id in self.events if number > sequence]
            return self.sequence, list(dict.fromkeys(runs))
