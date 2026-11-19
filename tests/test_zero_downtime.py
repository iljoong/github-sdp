import concurrent.futures
import importlib.util
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import threading
import time
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("demo_server_zdd", ROOT / "app" / "server.py")
server = importlib.util.module_from_spec(spec)
spec.loader.exec_module(server)


def printed_records(log):
    return [json.loads(call.args[0]) for call in log.call_args_list]


class LifecycleTests(unittest.TestCase):
    def setUp(self):
        self.now = [0.0]
        printer = patch("builtins.print")
        self.log = printer.start()
        self.addCleanup(printer.stop)

    def lifecycle(self, **env):
        settings = server.Settings.from_env({k: str(v) for k, v in env.items()})
        return server.Lifecycle(settings, clock=lambda: self.now[0])

    def test_settings_defaults_and_clamp(self):
        defaults = server.Settings.from_env({})
        self.assertEqual(defaults.long_task_seconds, 120)
        self.assertEqual(defaults.grace_seconds, 180)
        self.assertEqual(defaults.margin_seconds, 10)
        self.assertEqual(defaults.idle_seconds, 2)
        self.assertEqual(defaults.max_concurrent, 3)
        self.assertEqual(server.Settings.from_env({"LONG_TASK_SECONDS": "500"}).long_task_seconds, 165)
        self.assertEqual(server.Settings.from_env(
            {"GRACE_SECONDS": "600", "LONG_TASK_SECONDS": "500"}).long_task_seconds, 230)

    def test_settings_reject_invalid(self):
        for value in ("abc", "0", "601"):
            with self.subTest(value=value):
                with self.assertRaisesRegex(ValueError, "GRACE_SECONDS"):
                    server.Settings.from_env({"GRACE_SECONDS": value})
        with self.assertRaisesRegex(ValueError, "SHUTDOWN_MARGIN_SECONDS"):
            server.Settings.from_env({"GRACE_SECONDS": "10", "SHUTDOWN_MARGIN_SECONDS": "10"})
        with self.assertRaisesRegex(ValueError, "LONG_TASK_SECONDS"):
            server.Settings.from_env({"LONG_TASK_SECONDS": "soon"})

    def test_signal_during_locked_section_does_not_deadlock(self):
        lifecycle = self.lifecycle()
        handlers = {}
        with patch.object(server.signal, "signal", side_effect=lambda sig, fn: handlers.__setitem__(sig, fn)):
            server.install_signal_handlers(lifecycle)
        self.assertEqual(set(handlers), {server.signal.SIGTERM, server.signal.SIGINT})
        # The main thread may be inside should_exit() holding the lock when the signal lands.
        with lifecycle._lock:
            caller = threading.Thread(target=handlers[server.signal.SIGTERM], args=(server.signal.SIGTERM, None))
            caller.start()
            caller.join(1)
            self.assertFalse(caller.is_alive(), "signal handler blocked on the lifecycle lock")
        self.assertFalse(lifecycle.should_exit())
        self.assertEqual(lifecycle.state, "DRAINING")

    def test_drain_rejects_new_tasks_keeps_inflight(self):
        lifecycle = self.lifecycle()
        status, task = lifecycle.start_task()
        self.assertEqual(status, 200)
        self.assertTrue(task)
        self.assertTrue(lifecycle.begin_drain())
        self.assertFalse(lifecycle.begin_drain())
        self.assertEqual(lifecycle.start_task(), (503, None))
        state = lifecycle.status()
        self.assertIs(state["ready"], False)
        self.assertEqual(state["state"], "DRAINING")
        self.assertEqual(len(state["inflight"]), 1)

    def test_concurrency_cap(self):
        lifecycle = self.lifecycle()
        tasks = [lifecycle.start_task() for _ in range(3)]
        self.assertEqual([status for status, _ in tasks], [200, 200, 200])
        self.assertEqual(lifecycle.start_task(), (429, None))
        lifecycle.finish_task(tasks[0][1])
        self.assertEqual(lifecycle.start_task()[0], 200)

    def test_exit_after_last_task_plus_idle(self):
        lifecycle = self.lifecycle()
        _, task = lifecycle.start_task()
        lifecycle.begin_drain()
        self.now[0] = 50
        self.assertFalse(lifecycle.should_exit())
        self.now[0] = 120
        lifecycle.finish_task(task)
        self.now[0] = 121
        self.assertFalse(lifecycle.should_exit())
        self.now[0] = 122.1
        self.assertTrue(lifecycle.should_exit())
        self.assertEqual(lifecycle.state, "STOPPED")

    def test_exit_immediately_when_idle(self):
        lifecycle = self.lifecycle()
        self.assertFalse(lifecycle.should_exit())
        lifecycle.begin_drain()
        self.now[0] = 1
        self.assertFalse(lifecycle.should_exit())
        self.now[0] = 2.1
        self.assertTrue(lifecycle.should_exit())

    def test_exit_at_deadline_with_inflight(self):
        lifecycle = self.lifecycle()
        lifecycle.start_task()
        self.assertIsNone(lifecycle.status()["graceRemaining"])
        lifecycle.begin_drain()
        self.now[0] = 100
        self.assertEqual(lifecycle.status()["graceRemaining"], 80)
        self.now[0] = 169.9
        self.assertFalse(lifecycle.should_exit())
        self.now[0] = 170
        self.assertTrue(lifecycle.should_exit())

    def test_lifecycle_events_are_not_business(self):
        lifecycle = self.lifecycle()
        lifecycle.begin_drain()
        self.now[0] = 3
        lifecycle.should_exit()
        records = printed_records(self.log)
        self.assertEqual([r["event"] for r in records], ["draining", "stopped"])
        self.assertTrue(all(r["kind"] == "lifecycle" for r in records))


class EndpointTests(unittest.TestCase):
    def setUp(self):
        printer = patch("builtins.print")
        self.log = printer.start()
        self.addCleanup(printer.stop)
        self.lifecycle = server.Lifecycle(server.Settings.from_env({"LONG_TASK_SECONDS": "0.3"}))
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
        self.httpd.daemon_threads = True
        self.httpd.lifecycle = self.lifecycle
        thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        thread.start()
        self.base = f"http://127.0.0.1:{self.httpd.server_port}"

        def stop():
            self.httpd.shutdown()
            self.httpd.server_close()
            thread.join(2)
        self.addCleanup(stop)

    def call(self, path, method="GET"):
        request = urllib.request.Request(self.base + path, method=method,
                                         data=b"" if method == "POST" else None)
        try:
            with urllib.request.urlopen(request, timeout=5) as response:
                return response.status, dict(response.headers), response.read()
        except urllib.error.HTTPError as error:
            return error.code, dict(error.headers), error.read()

    def json(self, path, method="GET"):
        status, headers, body = self.call(path, method)
        return status, headers, json.loads(body)

    def test_readyz_flips_on_drain(self):
        self.assertEqual(self.json("/readyz")[0], 200)
        self.lifecycle.begin_drain()
        status, _, body = self.json("/readyz")
        self.assertEqual(status, 503)
        self.assertEqual(body["state"], "DRAINING")
        self.assertEqual(self.json("/healthz")[0], 200)

    def test_version_payload(self):
        with patch.dict(os.environ, {"APP_VERSION": "r7", "RELEASE_ID": "99",
                                     "CONTAINER_APP_REVISION": "app--r99",
                                     "CONTAINER_APP_ENV_DNS_SUFFIX": "x.koreacentral.azurecontainerapps.io"}):
            status, _, body = self.json("/api/version")
        self.assertEqual(status, 200)
        self.assertEqual(body["version"], "r7")
        self.assertEqual(body["release"], "99")
        self.assertEqual(body["revision"], "app--r99")
        self.assertEqual(body["dnsSuffix"], "x.koreacentral.azurecontainerapps.io")
        self.assertTrue(body["replica"])

    def test_status_reports_inflight(self):
        with concurrent.futures.ThreadPoolExecutor(1) as pool:
            task = pool.submit(self.json, "/api/long-task", "POST")
            for _ in range(100):
                if self.lifecycle.status()["inflight"]:
                    break
                time.sleep(0.01)
            status, _, body = self.json("/status")
            self.assertEqual(status, 200)
            self.assertEqual(body["state"], "SERVING")
            self.assertEqual(len(body["inflight"]), 1)
            self.assertIn("revision", body)
            task.result(5)

    def test_long_task_completes_and_reports_drain(self):
        with concurrent.futures.ThreadPoolExecutor(1) as pool:
            task = pool.submit(self.json, "/api/long-task", "POST")
            for _ in range(100):
                if self.lifecycle.status()["inflight"]:
                    break
                time.sleep(0.01)
            self.lifecycle.begin_drain()
            self.assertEqual(self.json("/api/long-task", "POST")[0], 503)
            status, _, body = task.result(5)
        self.assertEqual(status, 200)
        self.assertIs(body["completed"], True)
        self.assertIs(body["drainObserved"], True)
        self.assertGreaterEqual(body["durationMs"], 300)

    def test_long_task_cap_returns_429(self):
        for _ in range(3):
            self.lifecycle.start_task()
        self.assertEqual(self.json("/api/long-task", "POST")[0], 429)

    def test_client_disconnect_frees_slot(self):
        with socket.create_connection(("127.0.0.1", self.httpd.server_port)) as raw:
            raw.sendall(b"POST /api/long-task HTTP/1.1\r\nHost: x\r\nContent-Length: 0\r\n\r\n")
            for _ in range(100):
                if self.lifecycle.status()["inflight"]:
                    break
                time.sleep(0.01)
            self.assertTrue(self.lifecycle.status()["inflight"])
        deadline = time.monotonic() + 2
        while self.lifecycle.status()["inflight"] and time.monotonic() < deadline:
            time.sleep(0.05)
        self.assertEqual(self.lifecycle.status()["inflight"], [])

    def test_cors_only_on_api_and_status(self):
        status, headers, _ = self.call("/api/long-task", "OPTIONS")
        self.assertEqual(status, 204)
        self.assertEqual(headers.get("Access-Control-Allow-Origin"), "*")
        self.assertIn("POST", headers.get("Access-Control-Allow-Methods", ""))
        self.assertEqual(self.call("/status")[1].get("Access-Control-Allow-Origin"), "*")
        with patch.dict(os.environ, {"DEMO_LATENCY_MS": "0", "DEMO_FAILURE_PERCENT": "0"}):
            self.assertNotIn("Access-Control-Allow-Origin", self.call("/work")[1])
        self.assertEqual(self.call("/work", "OPTIONS")[0], 404)

    def test_verify_page_served(self):
        status, headers, body = self.call("/verify")
        self.assertEqual(status, 200)
        self.assertTrue(headers["Content-Type"].startswith("text/html"))
        self.assertIn(b"POLL_INTERVAL_MS", body)

    def test_new_endpoints_never_log_business(self):
        for path in ("/readyz", "/api/version", "/status", "/verify"):
            self.call(path)
        self.call("/api/long-task", "POST")
        self.call("/status", "OPTIONS")
        kinds = {r.get("kind") for r in printed_records(self.log)}
        self.assertNotIn("business", kinds)
        self.assertIn("long_task", kinds)


def free_port():
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


@unittest.skipUnless(os.name == "posix", "POSIX signal delivery required")
class ProcessSignalTests(unittest.TestCase):
    def test_sigterm_drains_then_exits(self):
        port = free_port()
        env = {**os.environ, "PORT": str(port), "LONG_TASK_SECONDS": "2", "GRACE_SECONDS": "20",
               "SHUTDOWN_MARGIN_SECONDS": "5", "DRAIN_IDLE_SECONDS": "0.5"}
        process = subprocess.Popen([sys.executable, str(ROOT / "app" / "server.py")], env=env,
                                   stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        self.addCleanup(lambda: process.poll() is None and process.kill())
        base = f"http://127.0.0.1:{port}"
        for _ in range(100):
            try:
                urllib.request.urlopen(base + "/healthz", timeout=1).close()
                break
            except OSError:
                time.sleep(0.05)
        with concurrent.futures.ThreadPoolExecutor(1) as pool:
            task = pool.submit(lambda: json.load(urllib.request.urlopen(
                urllib.request.Request(base + "/api/long-task", data=b"", method="POST"), timeout=10)))
            time.sleep(0.3)
            started = time.monotonic()
            process.terminate()
            time.sleep(0.2)
            with self.assertRaises(urllib.error.HTTPError) as caught:
                urllib.request.urlopen(base + "/readyz", timeout=2)
            self.assertEqual(caught.exception.code, 503)
            body = task.result(10)
        self.assertIs(body["drainObserved"], True)
        self.assertEqual(process.wait(6), 0)
        self.assertLess(time.monotonic() - started, 6)


class ArtifactTests(unittest.TestCase):
    def test_verify_page_declares_tunables_and_endpoints(self):
        page = (ROOT / "app" / "verify.html").read_text(encoding="utf-8")
        for needle in ("POLL_INTERVAL_MS: 1000", "REQUEST_TIMEOUT_MS", "/api/version",
                       "/status", "/api/long-task", "긴 작업 시작"):
            self.assertIn(needle, page)

    def test_dockerfile_bakes_version_and_page(self):
        dockerfile = (ROOT / "app" / "Dockerfile").read_text(encoding="utf-8")
        self.assertIn("ARG APP_VERSION", dockerfile)
        self.assertIn("ENV APP_VERSION=$APP_VERSION", dockerfile)
        self.assertIn("verify.html", dockerfile)
        release = (ROOT / ".github" / "workflows" / "release.yml").read_text(encoding="utf-8")
        self.assertIn("--build-arg APP_VERSION=", release)
        for script in ("bootstrap.sh", "bootstrap.ps1"):
            self.assertIn("--build-arg APP_VERSION=bootstrap",
                          (ROOT / "scripts" / script).read_text(encoding="utf-8"))
        self.assertNotIn("APP_VERSION", (ROOT / "infra" / "region.bicep").read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
