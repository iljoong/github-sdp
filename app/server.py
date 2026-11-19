"""Zero-downtime demo app; Python standard library only.

Endpoints
    GET  /                app intro + endpoint list with in-page Execute (index.html)
    GET  /healthz         liveness: always 200 while the process serves HTTP
    GET  /readyz          readiness: 200 while SERVING, 503 while DRAINING
    GET  /work            business contract + `kind: business` log (SDP health gate)
    GET  /api/version     version label, release, revision, replica, DNS suffix
    GET  /status          drain state, in-flight long tasks, grace countdown
    POST /api/long-task   ~LONG_TASK_SECONDS of work; result task_completed | task_incompleted
    GET  /verify          zero-downtime verification page (verify.html)
    OPTIONS /api/*, /status   CORS preflight (the page calls revision FQDNs)

Drain state machine
    SERVING --SIGTERM/SIGINT--> DRAINING --(no in-flight for DRAIN_IDLE_SECONDS)
                                         or (GRACE_SECONDS - SHUTDOWN_MARGIN_SECONDS)--> STOPPED (exit 0)
    While DRAINING: /readyz is 503 so the platform stops routing new requests,
    new long tasks get 503, in-flight long tasks keep running (or, with
    LONG_TASK_ABORT_ON_DRAIN=Y, stop and answer task_incompleted at once), HTTP keeps serving.

Environment (read once at startup; invalid values fail fast)
    APP_VERSION                dev   label shown on /verify (baked into the image)
    RELEASE_ID                 bootstrap
    LONG_TASK_SECONDS          120   capped at 230 (ingress timeout); longer than the drain
                                     deadline (GRACE - MARGIN) means it is cut off unless aborted
    GRACE_SECONDS              30    must equal template terminationGracePeriodSeconds (1..600)
    SHUTDOWN_MARGIN_SECONDS    10    exit this long before SIGKILL
    DRAIN_IDLE_SECONDS         2     linger after the last task so /verify can see it
    LONG_TASK_MAX_CONCURRENT   3     per replica; extra requests get 429
    LONG_TASK_ABORT_ON_DRAIN   N     Y: answer in-flight long tasks immediately on drain;
                                     anything else: run them to the end
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
    abort_on_drain: bool = False

    @classmethod
    def from_env(cls, env):
        grace = _number(env, "GRACE_SECONDS", 30, int)
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
        # Not capped by grace on purpose: a task longer than the grace lets the
        # demo show the drain outcome (deadline cut-off, or task_incompleted with abort).
        long_task = min(long_task, INGRESS_TIMEOUT_CAP_SECONDS)
        abort = env.get("LONG_TASK_ABORT_ON_DRAIN", "N").strip().upper() == "Y"
        return cls(long_task, grace, margin, idle, limit, abort)


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
        self._drain_requested = False

    @property
    def state(self):
        return self._state

    def log(self, event, **fields):
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
        self.log("draining", inflight=inflight, graceSeconds=self.settings.grace_seconds)
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
                "abortOnDrain": self.settings.abort_on_drain,
            }

    def request_drain(self):
        # Called from signal handlers: must not take self._lock (the main thread may hold it).
        self._drain_requested = True

    def should_exit(self):
        if self._drain_requested:
            self.begin_drain()
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
        self.log("stopped", reason="idle" if idle else "deadline", abandoned=abandoned)
        return True


DEFAULT_LIFECYCLE = Lifecycle(Settings.from_env({}))
VERIFY_PAGE = Path(__file__).resolve().parent / "verify.html"
PAGES = {"/": VERIFY_PAGE.parent / "index.html", "/verify": VERIFY_PAGE}
CORS_HEADERS = {"Access-Control-Allow-Origin": "*",
                "Access-Control-Allow-Methods": "GET, POST, OPTIONS",
                "Access-Control-Allow-Headers": "Content-Type",
                "Access-Control-Max-Age": "600"}


def replica_name():
    return os.environ.get("CONTAINER_APP_REPLICA_NAME") or socket.gethostname()


def cors_path(path):
    return path.startswith("/api/") or path == "/status"


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *_):
        pass

    @property
    def lifecycle(self):
        return getattr(self.server, "lifecycle", None) or DEFAULT_LIFECYCLE

    def send_body(self, status, payload, content_type="application/json", path=""):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Cache-Control", "no-store")
        if cors_path(path):
            for name, value in CORS_HEADERS.items():
                self.send_header(name, value)
        self.end_headers()
        self.wfile.write(payload)

    def send_json(self, status, body, path):
        self.send_body(status, json.dumps(body).encode(), path=path)

    def do_OPTIONS(self):
        path = self.path.split("?", 1)[0]
        if not cors_path(path):
            self.send_json(404, {"error": "not found"}, path)
            return
        self.send_response(204)
        for name, value in CORS_HEADERS.items():
            self.send_header(name, value)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_POST(self):
        path = self.path.split("?", 1)[0]
        length = int(self.headers.get("Content-Length") or 0)
        if length:
            self.rfile.read(min(length, 4096))
        if path != "/api/long-task":
            self.send_json(404, {"error": "not found"}, path)
            return
        self.long_task(path)

    def long_task(self, path):
        lifecycle = self.lifecycle
        status, task_id = lifecycle.start_task()
        revision = os.environ.get("CONTAINER_APP_REVISION", "local")
        if status != 200:
            reason = "draining" if status == 503 else "too many long tasks"
            self.send_json(status, {"error": reason, "revision": revision}, path)
            return
        started = time.monotonic()
        settings = lifecycle.settings
        duration = settings.long_task_seconds
        step = 0.2 if settings.abort_on_drain else 1.0
        completed = True
        self.log_task("start", task_id, revision)
        try:
            while (left := duration - (time.monotonic() - started)) > 0:
                if settings.abort_on_drain and lifecycle.state != "SERVING":
                    completed = False
                    break
                time.sleep(min(step, left))
        finally:
            lifecycle.finish_task(task_id)
        result = "task_completed" if completed else "task_incompleted"
        body = {"id": task_id, "revision": revision, "replica": replica_name(),
                "durationMs": round((time.monotonic() - started) * 1000), "completed": completed,
                "result": result, "drainObserved": lifecycle.state != "SERVING"}
        self.log_task("finish", task_id, revision, result=result, drainObserved=body["drainObserved"],
                      durationMs=body["durationMs"])
        self.send_json(200, body, path)

    @staticmethod
    def log_task(event, task_id, revision, **fields):
        print(json.dumps({"kind": "long_task", "event": event, "id": task_id, "time": utc_iso(),
                          "revision": revision, **fields}), flush=True)

    def demo_get(self, path, revision):
        """Returns (status, body) for demo endpoints, or None for other paths."""
        if path == "/readyz":
            body = self.lifecycle.status()
            return (200 if body["ready"] else 503), body
        if path == "/status":
            return 200, {**self.lifecycle.status(), "revision": revision, "replica": replica_name()}
        if path == "/api/version":
            return 200, {"version": os.environ.get("APP_VERSION", "dev"),
                         "release": os.environ.get("RELEASE_ID", "bootstrap"),
                         "revision": revision, "replica": replica_name(),
                         "dnsSuffix": os.environ.get("CONTAINER_APP_ENV_DNS_SUFFIX", ""),
                         "app": os.environ.get("CONTAINER_APP_NAME", "local"), "time": utc_iso()}
        return None

    def do_GET(self):
        started = time.monotonic()
        path = self.path.split("?", 1)[0]
        revision = os.environ.get("CONTAINER_APP_REVISION", "local")
        if path in PAGES:
            page = PAGES[path]
            if page.is_file():
                self.send_body(200, page.read_bytes(), "text/html; charset=utf-8")
            else:
                self.send_json(404, {"error": f"{page.name} missing"}, path)
            return
        demo = self.demo_get(path, revision)
        if demo is not None:
            self.send_json(*demo, path)
            return
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


def install_signal_handlers(lifecycle):
    def on_signal(_signum, _frame):
        lifecycle.request_drain()

    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, on_signal)


def run(settings, port):
    lifecycle = Lifecycle(settings)
    httpd = ThreadingHTTPServer(("0.0.0.0", port), Handler)
    # In-flight threads must not block exit once the grace deadline is reached.
    httpd.daemon_threads = True
    httpd.lifecycle = lifecycle
    install_signal_handlers(lifecycle)
    serving = threading.Thread(target=httpd.serve_forever, daemon=True)
    serving.start()
    lifecycle.log("serving", port=port, graceSeconds=settings.grace_seconds,
                   longTaskSeconds=settings.long_task_seconds)
    while not lifecycle.should_exit():
        time.sleep(0.2)
    httpd.shutdown()
    httpd.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(run(Settings.from_env(os.environ), int(os.environ.get("PORT", "8080"))))
