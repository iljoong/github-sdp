"""Bounded shared-endpoint demo load; never used for production traffic."""

import concurrent.futures
import json
import os
import re
import subprocess
import tempfile
import threading
import time
from dataclasses import dataclass

from common import iso, utcnow

WORKERS = 16
OUTSTANDING = 32
SATURATION_SECONDS = 30
DRAIN_SECONDS = 20
REQUEST_SECONDS = 10
PROCESS_SECONDS = 12
POLL_SECONDS = 0.05
LATENCY_BUCKETS = (0.01, 0.05, 0.1, 0.5, 1, 2, 5, 10, 15, 20)


class LoadCapacityError(RuntimeError):
    pass


class LoadDrainError(RuntimeError):
    pass


class LoadRequestError(RuntimeError):
    def __init__(self, category, message):
        super().__init__(message)
        self.category = category


def verify_transport():
    try:
        result = subprocess.run(
            ["curl", "--disable", "--version"], check=True, stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL, text=True, timeout=5)
    except (OSError, subprocess.SubprocessError):
        raise RuntimeError("Demo transport requires an available curl >= 8.4.0") from None
    version = re.match(r"curl (\d+)\.(\d+)\.(\d+)", result.stdout)
    if version is None or tuple(map(int, version.groups())) < (8, 4, 0):
        raise RuntimeError("Demo transport requires curl >= 8.4.0 for bounded response streaming")


def request(fqdn, run):
    # curl owns DNS, connect, TLS and body-read deadlines, including trickle bodies.
    # A separate process deadline also bounds a malfunctioning transport.
    with tempfile.TemporaryFile() as body:
        command = [
            "curl", "--disable", "--silent", "--max-time", str(REQUEST_SECONDS),
            "--connect-timeout", str(REQUEST_SECONDS), "--max-filesize", "65536",
            "--proto", "=http,https", "--output", "-", "--write-out", "%{stderr}%{http_code}",
            "--header", "X-Safe-Source: demo", "--header", f"X-Safe-Run: {run}",
            "--user-agent", "safe-deployment-example/1", "--url", f"https://{fqdn}/work",
        ]
        try:
            process = subprocess.Popen(command, stdout=body, stderr=subprocess.PIPE)
            try:
                _, status = process.communicate(timeout=PROCESS_SECONDS)
            except subprocess.TimeoutExpired:
                process.kill()
                try:
                    process.communicate(timeout=1)
                except subprocess.TimeoutExpired:
                    if process.stderr is not None:
                        process.stderr.close()
                    raise LoadRequestError("network_timeout", "Demo curl process could not be reaped after kill") from None
                raise LoadRequestError("network_timeout", "Demo request process deadline exceeded") from None
        except OSError:
            raise LoadRequestError("network_timeout", "Cannot start demo curl transport") from None
        if process.returncode:
            category = "response_parsing" if process.returncode == 63 else "network_timeout"
            raise LoadRequestError(category, f"Demo curl request failed (exit {process.returncode})")
        if status != b"200":
            raise LoadRequestError("http", "Demo endpoint returned a non-200 HTTP status")
        body.seek(0)
        try:
            value = json.loads(body.read(65537))
        except (ValueError, UnicodeError):
            raise LoadRequestError("response_parsing", "Demo response is not valid JSON") from None
        if not isinstance(value, dict) or value.get("ok") is not True or value.get("result") != 42:
            raise LoadRequestError("business_contract", "Demo business contract failed")


def histogram():
    return {"count": 0, "total_seconds": 0.0, "max_seconds": 0.0,
            "bucket_upper_seconds": list(LATENCY_BUCKETS) + [None],
            "bucket_counts": [0] * (len(LATENCY_BUCKETS) + 1)}


def observe(summary, value):
    summary["count"] += 1
    summary["total_seconds"] += value
    summary["max_seconds"] = max(summary["max_seconds"], value)
    bucket = next((i for i, limit in enumerate(LATENCY_BUCKETS) if value <= limit),
                  len(LATENCY_BUCKETS))
    summary["bucket_counts"][bucket] += 1


@dataclass
class Job:
    identifier: int
    submitted: float
    started: float | None = None
    finished: float | None = None
    error: Exception | None = None
    cancelled: bool = False


class Generator:
    def __init__(self, stop, fqdn, run, stats):
        self.stop, self.fqdn, self.run_id, self.stats = stop, fqdn, run, stats
        self.started = time.monotonic()
        started_at = iso(utcnow())
        self.failure_lock = threading.Lock()
        self.failures = []
        self.pending = {}
        self.saturated_since = None
        self.pressure_updated = self.started
        self.interval_counts = {}
        self.reported_intervals = 0
        commit = os.environ.get("GITHUB_SHA")
        if not commit:
            commit = subprocess.check_output(
                ["git", "rev-parse", "HEAD"], cwd=os.path.dirname(__file__),
                stderr=subprocess.DEVNULL, timeout=5, text=True).strip()
        stats.update({
            "schema_version": 2, "target_rps": 10, "submitted": 0, "completed": 0,
            "succeeded": 0, "failed": 0, "cancelled_before_start": 0, "unresolved": 0,
            "outstanding": 0, "high_water_outstanding": 0,
            "started_at": started_at, "generator_state": "STARTING",
            "implementation_commit": commit,
            "intervals": [], "queue_delay": histogram(), "request_duration": histogram(),
            "failure_categories": {name: 0 for name in (
                "network_timeout", "http", "response_parsing", "business_contract", "internal")},
            "saturation_episodes": 0, "saturation_seconds": 0.0,
            "continuous_saturation_seconds": 0.0, "max_continuous_saturation_seconds": 0.0,
        })

    def failure(self, error, source, at=None, request_id=None):
        with self.failure_lock:
            self.failures.append((time.monotonic() if at is None else at, error, source, request_id))
            self.failures.sort(key=lambda item: (item[0], item[3] or 0))

    def first_error(self):
        with self.failure_lock:
            return self.failures[0][1] if self.failures else None

    def check(self):
        error = self.first_error()
        if error is not None:
            raise error

    def execute(self, job):
        if self.stop.is_set():
            job.cancelled = True
            job.finished = time.monotonic()
            return
        job.started = time.monotonic()
        try:
            request(self.fqdn, self.run_id)
        except Exception as error:
            job.error = error
        finally:
            job.finished = time.monotonic()
            if job.error is not None:
                self.failure(job.error, "request", job.finished, job.identifier)
                self.stop.set()

    def interval(self, at):
        index = int(max(0, at - self.started) // 60)
        if index < self.reported_intervals:
            return self.stats["intervals"][index]
        return self.interval_counts.setdefault(index, {
            "submitted": 0, "succeeded": 0, "failed": 0, "cancelled_before_start": 0,
            "queue_delay": histogram(), "request_duration": histogram(),
            "saturation_seconds": 0.0, "high_water_outstanding": 0})

    def count(self, name, at):
        counts = self.interval(at)
        counts[name] += 1
        self.stats[name] += 1
        if "duration_seconds" in counts:
            counts["achieved_rps"] = counts["succeeded"] / counts["duration_seconds"]
            counts["submitted_rps"] = counts["submitted"] / counts["duration_seconds"]

    def collect(self):
        for future, job in list(self.pending.items()):
            if not future.done():
                continue
            del self.pending[future]
            if future.cancelled():
                job.cancelled = True
                job.finished = time.monotonic()
            else:
                try:
                    future.result()
                except BaseException as error:
                    self.failure(error, "worker", request_id=job.identifier)
                    job.error = RuntimeError("Demo worker terminated unexpectedly")
                    job.finished = time.monotonic()
            finished = job.finished if job.finished is not None else time.monotonic()
            outcome = "cancelled_before_start" if job.cancelled else "failed" if job.error else "succeeded"
            self.count(outcome, finished)
            if job.error:
                category = getattr(job.error, "category", "internal")
                self.stats["failure_categories"][category] += 1
            if job.started is not None:
                for summary in (self.stats, self.interval(finished)):
                    observe(summary["queue_delay"], job.started - job.submitted)
                    observe(summary["request_duration"], finished - job.submitted)
            self.stats["completed"] = self.stats["succeeded"]
        self.stats["outstanding"] = len(self.pending)

    def pressure(self, now, full):
        if self.saturated_since is not None:
            begin = self.pressure_updated
            while begin < now:
                boundary = self.started + (int((begin - self.started) // 60) + 1) * 60
                end = min(now, boundary)
                self.interval(begin)["saturation_seconds"] += end - begin
                begin = end
            self.stats["saturation_seconds"] += now - self.pressure_updated
        self.pressure_updated = now
        if full and self.saturated_since is None:
            self.saturated_since = now
            self.stats["saturation_episodes"] += 1
        continuous = now - self.saturated_since if self.saturated_since is not None else 0.0
        self.stats["continuous_saturation_seconds"] = continuous
        self.stats["max_continuous_saturation_seconds"] = max(
            self.stats["max_continuous_saturation_seconds"], continuous)
        if not full and self.saturated_since is not None:
            self.saturated_since = None
            self.stats["continuous_saturation_seconds"] = 0.0
        return continuous

    def report(self, now, final=False):
        elapsed = now - self.started
        while self.reported_intervals < int(elapsed // 60):
            index = self.reported_intervals
            record = self.interval(self.started + index * 60)
            self.interval_counts.pop(index)
            record.update({"start_elapsed_seconds": index * 60, "duration_seconds": 60,
                      "complete": True, "target_rps": 10,
                      "achieved_rps": record["succeeded"] / 60,
                      "submitted_rps": record["submitted"] / 60,
                      "outstanding": self.stats["outstanding"],
                      "cumulative_saturation_seconds": self.stats["saturation_seconds"],
                      "continuous_saturation_seconds": self.stats["continuous_saturation_seconds"]})
            self.stats["intervals"].append(record)
            self.reported_intervals += 1
            print(json.dumps({"demo_load_interval": record}), flush=True)
        if final:
            duration = elapsed - self.reported_intervals * 60
            if duration > 0 or self.reported_intervals in self.interval_counts:
                record = self.interval(now)
                self.interval_counts.pop(self.reported_intervals)
                record.update({
                    "start_elapsed_seconds": self.reported_intervals * 60,
                    "duration_seconds": duration, "complete": False, "target_rps": 10,
                    "achieved_rps": record["succeeded"] / duration if duration else 0,
                    "submitted_rps": record["submitted"] / duration if duration else 0,
                    "outstanding": self.stats["outstanding"],
                    "cumulative_saturation_seconds": self.stats["saturation_seconds"],
                    "continuous_saturation_seconds": self.stats["continuous_saturation_seconds"]})
                self.stats["intervals"].append(record)

    def run(self):
        pool = concurrent.futures.ThreadPoolExecutor(max_workers=WORKERS, thread_name_prefix="demo-request")
        next_request = time.monotonic()
        self.stats["generator_state"] = "RUNNING"
        try:
            while not self.stop.is_set():
                self.collect()
                self.check()
                now = time.monotonic()
                full = len(self.pending) >= OUTSTANDING
                continuous = self.pressure(now, full)
                self.report(now)
                if full:
                    self.stats["generator_state"] = "BACKPRESSURED"
                    if continuous >= SATURATION_SECONDS:
                        raise LoadCapacityError("Demo load capacity saturated for 30 seconds")
                    next_request = now + 0.1
                    self.stop.wait(min(POLL_SECONDS, SATURATION_SECONDS - continuous))
                    continue
                self.stats["generator_state"] = "RUNNING"
                if now < next_request:
                    self.stop.wait(next_request - now)
                    continue
                # stop may have been set while harvesting completed requests.
                if self.stop.is_set():
                    break
                job = Job(self.stats["submitted"] + 1, now)
                future = pool.submit(self.execute, job)
                self.pending[future] = job
                self.count("submitted", now)
                self.stats["outstanding"] = len(self.pending)
                self.stats["high_water_outstanding"] = max(
                    self.stats["high_water_outstanding"], len(self.pending))
                interval = self.interval(now)
                interval["high_water_outstanding"] = max(
                    interval["high_water_outstanding"], len(self.pending))
                next_request = time.monotonic() + 0.1
        except BaseException as error:
            if error is not self.first_error():
                self.failure(error, "generator")
            self.stats["generator_state"] = "FAILED"
        finally:
            self.stop.set()
            self.stats["generator_state"] = "STOPPING"
            stopped = time.monotonic()
            self.pressure(stopped, False)
            for future in self.pending:
                future.cancel()
            self.stats["generator_state"] = "DRAINING"
            while self.pending and time.monotonic() - stopped < DRAIN_SECONDS:
                self.collect()
                if self.pending:
                    time.sleep(POLL_SECONDS)
            self.collect()
            self.stats["unresolved"] = len(self.pending)
            self.stats["unresolved_request_ids"] = [job.identifier for job in self.pending.values()]
            if self.pending:
                self.failure(LoadDrainError("Demo requests unresolved at shutdown deadline"), "drain")
            pool.shutdown(wait=not self.pending, cancel_futures=True)
            self.stats["shutdown"] = {
                "outcome": "deadline_exceeded" if self.pending else "drained",
                "budget_seconds": DRAIN_SECONDS, "elapsed_seconds": time.monotonic() - stopped,
                "running_threads_terminated": False,
            }
            finished = time.monotonic()
            self.report(finished, final=True)
            elapsed = finished - self.started
            self.stats.update({"generator_state": "STOPPED", "finished_at": iso(utcnow()),
                               "elapsed_seconds": elapsed,
                               "scheduling_seconds": stopped - self.started,
                               "achieved_rps": self.stats["completed"] / elapsed if elapsed else 0,
                               "submitted_rps": self.stats["submitted"] / elapsed if elapsed else 0})

    def finish_evidence(self):
        with self.failure_lock:
            failures = [
                {"type": type(error).__name__, "message": str(error), "source": source,
                 "elapsed_seconds": at - self.started, "request_id": identifier}
                for at, error, source, identifier in self.failures]
        self.stats.update({"primary_failure": failures[0] if failures else None,
                           "secondary_failures": failures[1:],
                           "status": "failed" if failures else "completed"})
        if failures:
            self.stats["error"] = failures[0]["message"]
