"""Small observable business endpoint; no external Python dependencies."""
import json
import os
import random
import time
import uuid
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


def business_result(failure_percent=0):
    return random.random() * 100 >= failure_percent


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
