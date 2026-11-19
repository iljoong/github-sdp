"""Zero-downtime demo app; Python standard library only.

Endpoints
    GET  /healthz         liveness: always 200 while the process serves HTTP
    GET  /readyz          readiness: 200 while SERVING, 503 while DRAINING
    GET  /work            business contract + `kind: business` log (SDP health gate)
    GET  /api/version     version label, release, revision, replica, DNS suffix
    GET  /status          drain state, in-flight long tasks, grace countdown
    POST /api/long-task   ~LONG_TASK_SECONDS of work that survives SIGTERM
    GET  /verify          zero-downtime verification page (verify.html)
    OPTIONS /api/*, /status   CORS preflight (the page calls revision FQDNs)

Drain state machine
    SERVING --SIGTERM/SIGINT--> DRAINING --(no in-flight for DRAIN_IDLE_SECONDS)
                                         or (GRACE_SECONDS - SHUTDOWN_MARGIN_SECONDS)--> STOPPED (exit 0)
    While DRAINING: /readyz is 503 so the platform stops routing new requests,
    new long tasks get 503, in-flight long tasks keep running, HTTP keeps serving.

Environment (read once at startup; invalid values fail fast)
    APP_VERSION                dev   label shown on /verify (baked into the image)
    RELEASE_ID                 bootstrap
    LONG_TASK_SECONDS          120   clamped to min(value, GRACE - MARGIN - 5, 230)
    GRACE_SECONDS              180   must equal template terminationGracePeriodSeconds (1..600)
    SHUTDOWN_MARGIN_SECONDS    10    exit this long before SIGKILL
    DRAIN_IDLE_SECONDS         2     linger after the last task so /verify can see it
    LONG_TASK_MAX_CONCURRENT   3     per replica; extra requests get 429
    DEMO_LATENCY_MS / DEMO_FAILURE_PERCENT   /work tuning (unchanged)
"""
import json
import os
import random
import signal
import socket
import sys
import threading
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

INGRESS_TIMEOUT_CAP_SECONDS = 230  # ACA ingress request timeout is 240 s


def business_result(failure_percent=0):
    return random.random() * 100 >= failure_percent


def utc_iso():
    return datetime.now(timezone.utc).isoformat()


def _number(env, name, default, kind=float):
    raw = env.get(name, str(default))
    try:
        return kind(raw)
    except ValueError:
        raise ValueError(f"{name} must be a number, got {raw!r}") from None


@dataclass(frozen=True)
class Settings:
    long_task_seconds: float
    grace_seconds: int
    margin_seconds: int
    idle_seconds: float
    max_concurrent: int

    @classmethod
    def from_env(cls, env):
        grace = _number(env, "GRACE_SECONDS", 180, int)
        margin = _number(env, "SHUTDOWN_MARGIN_SECONDS", 10, int)
        long_task = _number(env, "LONG_TASK_SECONDS", 120)
        idle = _number(env, "DRAIN_IDLE_SECONDS", 2)
        limit = _number(env, "LONG_TASK_MAX_CONCURRENT", 3, int)
        if not 1 <= grace <= 600:
            raise ValueError(f"GRACE_SECONDS must be 1..600, got {grace}")
        if not 0 <= margin < grace:
            raise ValueError(f"SHUTDOWN_MARGIN_SECONDS must be 0..GRACE_SECONDS-1, got {margin}")
        if long_task < 0 or idle < 0 or limit < 1:
            raise ValueError("LONG_TASK_SECONDS/DRAIN_IDLE_SECONDS must be >= 0 and "
                             "LONG_TASK_MAX_CONCURRENT >= 1")
        long_task = min(long_task, max(grace - margin - 5, 0), INGRESS_TIMEOUT_CAP_SECONDS)
        return cls(long_task, grace, margin, idle, limit)


class Lifecycle:
    """Thread-safe drain state shared by the HTTP handler and the main loop."""

    def __init__(self, settings, clock=time.monotonic):
        self.settings = settings
        self._clock = clock
        self._lock = threading.Lock()
        self._state = "SERVING"
        self._drain_started = None
        self._drain_started_at = None
        self._last_finish = None
        self._tasks = {}

    @property
    def state(self):
        return self._state

    def _log(self, event, **fields):
        print(json.dumps({"kind": "lifecycle", "event": event, "time": utc_iso(),
                          "revision": os.environ.get("CONTAINER_APP_REVISION", "local"),
                          **fields}), flush=True)

    def begin_drain(self):
        with self._lock:
            if self._state != "SERVING":
                return False
            self._state = "DRAINING"
            self._drain_started = self._clock()
            self._drain_started_at = utc_iso()
            inflight = len(self._tasks)
        self._log("draining", inflight=inflight, graceSeconds=self.settings.grace_seconds)
        return True

    def start_task(self):
        with self._lock:
            if self._state != "SERVING":
                return 503, None
            if len(self._tasks) >= self.settings.max_concurrent:
                return 429, None
            task_id = uuid.uuid4().hex[:8]
            self._tasks[task_id] = (self._clock(), utc_iso())
            return 200, task_id

    def finish_task(self, task_id):
        with self._lock:
            if self._tasks.pop(task_id, None) is not None:
                self._last_finish = self._clock()

    def status(self):
        with self._lock:
            now = self._clock()
            remaining = None
            if self._drain_started is not None:
                remaining = max(0.0, round(self._drain_started + self.settings.grace_seconds - now, 1))
            return {
                "state": self._state, "ready": self._state == "SERVING",
                "inflight": [{"id": key, "startedAt": started_at, "elapsed": round(now - started, 1)}
                             for key, (started, started_at) in self._tasks.items()],
                "drainStartedAt": self._drain_started_at,
                "graceSeconds": self.settings.grace_seconds, "graceRemaining": remaining,
                "longTaskSeconds": self.settings.long_task_seconds,
            }

    def should_exit(self):
        with self._lock:
            if self._state == "STOPPED":
                return True
            if self._state != "DRAINING":
                return False
            now = self._clock()
            deadline = self._drain_started + self.settings.grace_seconds - self.settings.margin_seconds
            quiet_since = max(self._drain_started, self._last_finish or self._drain_started)
            idle = not self._tasks and now - quiet_since >= self.settings.idle_seconds
            if not (idle or now >= deadline):
                return False
            self._state = "STOPPED"
            abandoned = len(self._tasks)
        self._log("stopped", reason="idle" if idle else "deadline", abandoned=abandoned)
        return True


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *_):
        pass

    def do_GET(self):
        started = time.monotonic()
        path = self.path.split("?", 1)[0]
        revision = os.environ.get("CONTAINER_APP_REVISION", "local")
        if path == "/healthz":
            status, body = 200, {"ready": True, "revision": revision}
        elif path == "/work":
            time.sleep(float(os.environ.get("DEMO_LATENCY_MS", "15")) / 1000)
            ok = business_result(float(os.environ.get("DEMO_FAILURE_PERCENT", "0")))
            status = 200 if ok else 503
            body = {"ok": ok, "result": 42 if ok else None, "revision": revision,
                    "release": os.environ.get("RELEASE_ID", "bootstrap")}
        else:
            status, body = 404, {"error": "not found"}
        payload = json.dumps(body).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        try:
            self.wfile.write(payload)
        finally:
            if path == "/work":
                # Headers are for this educational sample, NOT an authenticated
                # source of telemetry classification in a hostile public service.
                source = self.headers.get("X-Safe-Source", "organic")
                if source not in {"organic", "demo", "control", "probe"}:
                    source = "organic"
                print(json.dumps({
                    "kind": "business", "event_time": datetime.now(timezone.utc).isoformat(),
                    "request_id": uuid.uuid4().hex, "revision": revision,
                    "release": os.environ.get("RELEASE_ID", "bootstrap"),
                    "monitor_run": self.headers.get("X-Safe-Run", ""),
                    "source": source, "status": status, "business_ok": body["ok"],
                    "duration_ms": round((time.monotonic() - started) * 1000, 3),
                }), flush=True)


if __name__ == "__main__":
    ThreadingHTTPServer(("0.0.0.0", int(os.environ.get("PORT", "8080"))), Handler).serve_forever()
