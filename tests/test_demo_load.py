import concurrent.futures
import json
import os
from pathlib import Path
import signal
import socketserver
import subprocess
import sys
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import demo_load
import rollout


class VirtualLoad:
    """Drive real scheduler logic with controlled capacity and monotonic time."""

    def __init__(self, until, recoveries=(), complete_on_stop=False):
        self.now = 0.0
        self.until = until
        self.recoveries = list(recoveries)
        self.complete_on_stop = complete_on_stop
        self.stop = threading.Event()
        self.jobs = []
        self.submissions = []
        self.stats = {}

    def submit(self, fn, job):
        future = concurrent.futures.Future()
        if sum(f.running() for f, _ in self.jobs) < demo_load.WORKERS:
            future.set_running_or_notify_cancel()
            job.started = self.now
        self.jobs.append((future, job))
        self.submissions.append(self.now)
        return future

    def complete(self):
        for future, job in self.jobs:
            if future.done():
                continue
            if job.started is None:
                job.started = self.now
            job.finished = self.now
            future.set_result(None)

    def advance(self, delay):
        self.now += delay
        if self.recoveries and self.now >= self.recoveries[0]:
            self.recoveries.pop(0)
            self.complete()
        if self.now >= self.until:
            self.stop.set()
            if self.complete_on_stop:
                self.complete()

    def run(self):
        pool = Mock(submit=self.submit)
        with patch.dict(os.environ, {"GITHUB_SHA": "test-commit"}), \
             patch.object(demo_load.time, "monotonic", side_effect=lambda: self.now), \
             patch.object(demo_load.time, "sleep", side_effect=self.advance), \
             patch.object(self.stop, "wait", side_effect=self.advance), \
             patch.object(demo_load.concurrent.futures, "ThreadPoolExecutor", return_value=pool), \
             patch("builtins.print"):
            generator = demo_load.Generator(self.stop, "shared.example", "123", self.stats)
            generator.run()
            generator.finish_evidence()
        return generator, pool


class LoadAccountingTests(unittest.TestCase):
    def assert_reconciled(self, stats):
        self.assertEqual(stats["submitted"], sum(stats[name] for name in (
            "succeeded", "failed", "cancelled_before_start", "unresolved")))
        self.assertEqual(stats["completed"], stats["succeeded"])

    def test_repeated_short_pressure_resumes_without_catchup_bursts(self):
        load = VirtualLoad(19, recoveries=(8, 16), complete_on_stop=True)
        generator, _ = load.run()
        generator.check()
        stats = load.stats
        self.assertEqual(stats["saturation_episodes"], 2)
        self.assertGreater(stats["saturation_seconds"], 8)
        self.assertLess(stats["max_continuous_saturation_seconds"], 6)
        self.assertEqual(stats["continuous_saturation_seconds"], 0)
        self.assertEqual(stats["high_water_outstanding"], 32)
        self.assertTrue(all(b - a >= 0.099 for a, b in zip(load.submissions, load.submissions[1:])))
        self.assertAlmostEqual(sum(i["saturation_seconds"] for i in stats["intervals"]),
                               stats["saturation_seconds"])
        self.assertEqual(stats["status"], "completed")
        self.assert_reconciled(stats)

    def test_stop_cancels_queued_tasks_and_drains_running_requests(self):
        load = VirtualLoad(4, recoveries=(5,))
        generator, pool = load.run()
        generator.check()
        stats = load.stats
        self.assertEqual(stats["submitted"], 32)
        self.assertEqual(stats["cancelled_before_start"], 16)
        self.assertEqual(stats["succeeded"], 16)
        self.assertEqual(stats["unresolved"], 0)
        self.assertLess(stats["shutdown"]["elapsed_seconds"], 2)
        self.assertLessEqual(max(load.submissions), 4)
        self.assertEqual(stats["queue_delay"]["count"], 16)
        self.assertEqual(stats["request_duration"]["count"], 16)
        pool.shutdown.assert_called_once_with(wait=True, cancel_futures=True)
        self.assert_reconciled(stats)

    def test_capacity_failure_occurs_at_deadline_and_unresolved_is_explicit(self):
        load = VirtualLoad(100)
        generator, pool = load.run()
        with self.assertRaises(demo_load.LoadCapacityError):
            generator.check()
        self.assertEqual(len(load.submissions), 32)
        self.assertAlmostEqual(load.stats["primary_failure"]["elapsed_seconds"],
                               load.submissions[-1] + 30)
        self.assertAlmostEqual(load.stats["shutdown"]["elapsed_seconds"], 20, delta=0.051)
        self.assertEqual(load.stats["unresolved"], 16)
        self.assertEqual(len(load.stats["unresolved_request_ids"]), 16)
        self.assertFalse(load.stats["shutdown"]["running_threads_terminated"])
        self.assertEqual(load.stats["secondary_failures"][0]["type"], "LoadDrainError")
        pool.shutdown.assert_called_once_with(wait=False, cancel_futures=True)
        self.assert_reconciled(load.stats)

    def test_late_observed_completion_updates_correct_complete_interval(self):
        stats = {}
        with patch.dict(os.environ, {"GITHUB_SHA": "test-commit"}), \
             patch.object(demo_load.time, "monotonic", return_value=0), patch("builtins.print"):
            generator = demo_load.Generator(threading.Event(), "shared", "123", stats)
            generator.count("submitted", 59)
            generator.report(60)
            job = demo_load.Job(1, 59, started=59.1, finished=59.9)
            future = concurrent.futures.Future()
            future.set_result(None)
            generator.pending[future] = job
            generator.collect()
        interval = stats["intervals"][0]
        self.assertEqual(interval["succeeded"], 1)
        self.assertEqual(interval["achieved_rps"], 1 / 60)
        self.assertEqual(interval["request_duration"]["count"], 1)
        self.assertAlmostEqual(stats["queue_delay"]["total_seconds"], 0.1)
        self.assertAlmostEqual(stats["request_duration"]["total_seconds"], 0.9)

    def test_controller_primary_survives_multiple_worker_and_evidence_errors(self):
        release = threading.Event()
        entered = threading.Event()
        counter = [0]
        lock = threading.Lock()
        controller_error = RuntimeError("original gate failure")
        original_failure = demo_load.Generator.failure

        def fail_request(*args):
            with lock:
                counter[0] += 1
                if counter[0] >= 2:
                    entered.set()
            self.assertTrue(release.wait(3))
            raise demo_load.LoadRequestError("http", "Demo endpoint returned a non-200 HTTP status")

        def failure(generator, error, source, *args, **kwargs):
            original_failure(generator, error, source, *args, **kwargs)
            if source == "controller":
                release.set()

        with patch.dict(os.environ, {"REGION": "kr", "GITHUB_SHA": "test-commit"}), \
             patch.object(demo_load, "request", side_effect=fail_request), \
             patch.object(demo_load.Generator, "failure", new=failure), \
             patch.object(rollout, "save", side_effect=OSError("evidence unavailable")), \
             patch("builtins.print") as output:
            with self.assertRaises(RuntimeError) as caught:
                with rollout.demo_traffic("demo", {"run_id": "123"}, "shared", 10):
                    self.assertTrue(entered.wait(3))
                    raise controller_error
        self.assertIs(caught.exception, controller_error)
        record = json.loads(output.call_args.args[0])["demo_load"]
        self.assertEqual(record["primary_failure"]["source"], "controller")
        self.assertGreaterEqual(record["failed"], 2)
        self.assertEqual(record["failure_categories"]["http"], record["failed"])
        self.assertEqual(record["secondary_failures"][-1]["source"], "evidence")
        self.assertEqual(len([f for f in record["secondary_failures"] if f["source"] == "request"]),
                         record["failed"])
        self.assert_reconciled(record)

    def test_generator_failure_precedes_later_controller_error(self):
        failed = threading.Event()
        original_failure = demo_load.Generator.failure
        load_error = demo_load.LoadRequestError("business_contract", "business contract failed")

        def failure(generator, error, source, *args, **kwargs):
            original_failure(generator, error, source, *args, **kwargs)
            if source == "request":
                failed.set()

        with patch.dict(os.environ, {"REGION": "kr", "GITHUB_SHA": "test-commit"}), \
             patch.object(demo_load, "request", side_effect=load_error), \
             patch.object(demo_load.Generator, "failure", new=failure), \
             patch.object(rollout, "save") as save, patch("builtins.print"):
            with self.assertRaises(demo_load.LoadRequestError) as caught:
                with rollout.demo_traffic("demo", {"run_id": "123"}, "shared", 10):
                    self.assertTrue(failed.wait(3))
                    raise RuntimeError("later monitoring failure")
        self.assertIs(caught.exception, load_error)
        record = save.call_args.args[1]
        self.assertEqual(record["primary_failure"]["source"], "request")
        self.assertEqual(record["secondary_failures"][0]["source"], "controller")
        self.assert_reconciled(record)

    def test_evidence_failure_alone_prevents_success(self):
        with patch.dict(os.environ, {"REGION": "kr", "GITHUB_SHA": "test-commit"}), \
             patch.object(demo_load, "request"), \
             patch.object(rollout, "save", side_effect=OSError("disk full")), \
             patch("builtins.print") as output:
            with self.assertRaisesRegex(OSError, "disk full"):
                with rollout.demo_traffic("demo", {"run_id": "123"}, "shared", 10):
                    pass
        record = json.loads(output.call_args.args[0])["demo_load"]
        self.assertEqual(record["primary_failure"]["source"], "evidence")
        self.assert_reconciled(record)

    def test_sigint_and_sigterm_stop_drain_and_preserve_failure(self):
        for sig in (signal.SIGINT, signal.SIGTERM):
            with self.subTest(signal=sig), \
                 patch.dict(os.environ, {"REGION": "kr", "GITHUB_SHA": "test-commit"}), \
                 patch.object(demo_load, "request"), \
                 patch.object(rollout, "save") as save, patch("builtins.print"):
                original = signal.signal(sig, rollout.cancelled)
                try:
                    with self.assertRaisesRegex(RuntimeError, "Runner signal"):
                        with rollout.demo_traffic("demo", {"run_id": "123"}, "shared", 10):
                            signal.raise_signal(sig)
                finally:
                    signal.signal(sig, original)
                record = save.call_args.args[1]
                self.assertEqual(record["generator_state"], "STOPPED")
                self.assertEqual(record["shutdown"]["outcome"], "drained")
                self.assertEqual(record["primary_failure"]["source"], "controller")
                self.assert_reconciled(record)
        self.assertFalse(any(t.name.startswith("demo-request") for t in threading.enumerate()))

    def test_production_does_not_create_generator_or_evidence(self):
        with patch.object(demo_load, "Generator") as generator, patch.object(rollout, "save") as save:
            with rollout.demo_traffic("production", {}, "shared", 10) as check:
                check()
            generator.assert_not_called()
            save.assert_not_called()


class TransportTests(unittest.TestCase):
    def test_transport_preflight_requires_curl_with_streaming_size_limit(self):
        for version, valid in (("8.4.0", True), ("8.5.0", True), ("9.0.0", True),
                               ("8.3.0", False), ("7.88.1", False), ("invalid", False)):
            with self.subTest(version=version), \
                 patch.object(demo_load.subprocess, "run",
                              return_value=Mock(stdout=f"curl {version} test")):
                if valid:
                    demo_load.verify_transport()
                else:
                    with self.assertRaisesRegex(RuntimeError, "curl >= 8.4.0"):
                        demo_load.verify_transport()
        with patch.object(demo_load.subprocess, "run", side_effect=FileNotFoundError):
            with self.assertRaisesRegex(RuntimeError, "available curl"):
                demo_load.verify_transport()

    def test_process_deadline_kills_stalled_transport_including_dns_or_connect(self):
        process = Mock()
        process.communicate.side_effect = [subprocess.TimeoutExpired("curl", 12), (None, b"")]
        with patch.object(demo_load.subprocess, "Popen", return_value=process) as popen:
            with self.assertRaisesRegex(demo_load.LoadRequestError, "process deadline"):
                demo_load.request("shared.example", "123")
        process.kill.assert_called_once()
        self.assertEqual([call.kwargs["timeout"] for call in process.communicate.call_args_list], [12, 1])
        command = popen.call_args.args[0]
        self.assertEqual(command[command.index("--max-time") + 1], "10")
        self.assertEqual(command[command.index("--connect-timeout") + 1], "10")
        self.assertEqual(command[-1], "https://shared.example/work")
        self.assertLess(demo_load.PROCESS_SECONDS + 1, demo_load.DRAIN_SECONDS)

    def test_process_reap_deadline_is_not_replaced_by_unbounded_context_exit(self):
        process = Mock()
        process.communicate.side_effect = subprocess.TimeoutExpired("curl", 1)
        with patch.object(demo_load.subprocess, "Popen", return_value=process):
            with self.assertRaisesRegex(demo_load.LoadRequestError, "could not be reaped"):
                demo_load.request("shared.example", "123")
        process.kill.assert_called_once()
        process.wait.assert_not_called()

    def test_missing_curl_is_explicit_failure(self):
        with patch.object(demo_load.subprocess, "Popen", side_effect=FileNotFoundError):
            with self.assertRaisesRegex(demo_load.LoadRequestError, "Cannot start"):
                demo_load.request("shared.example", "123")

    def test_real_curl_status_parsing_contract_and_body_limit(self):
        for status, body, category in (
            (200, b'{"ok":true,"result":42}', None),
            (503, b'{"ok":true,"result":42}', "http"),
            (200, b"not json: private response", "response_parsing"),
            (200, b'{"ok":false,"result":42}', "business_contract"),
            (200, b"[]", "business_contract"),
            (200, b"x" * 70000, "response_parsing"),
        ):
            with self.subTest(status=status, category=category):
                self.serve_request(status, body, category=category)

    def test_real_curl_slow_headers_body_and_trickle_obey_total_deadline(self):
        for mode in ("headers", "body", "trickle"):
            with self.subTest(mode=mode):
                self.serve_request(200, b'{"ok":true,"result":42}', mode=mode,
                                   category="network_timeout")

    def test_real_curl_unknown_length_body_is_size_bounded(self):
        self.serve_request(200, b"x" * 70000, category="response_parsing", content_length=False)

    def test_real_scheduler_and_curl_stage_persist_reconciled_evidence(self):
        self.serve_request(200, b'{"ok":true,"result":42}', stage=True)

    def serve_request(self, status, body, mode=None, category=None, content_length=True, stage=False):
        done = threading.Event()

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                try:
                    if mode == "headers":
                        done.wait(1)
                    self.send_response(status)
                    if content_length:
                        self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    if mode == "body":
                        done.wait(1)
                    if mode == "trickle":
                        for byte in body:
                            self.wfile.write(bytes([byte]))
                            self.wfile.flush()
                            if done.wait(0.05):
                                break
                    else:
                        self.wfile.write(body)
                except (BrokenPipeError, ConnectionResetError):
                    pass  # Expected when the client's operation deadline closes the connection.

            def log_message(self, *args):
                pass

        real_popen = subprocess.Popen

        def local_http(command, **kwargs):
            # Only substitute local plaintext HTTP; all curl deadline flags stay intact.
            command = list(command)
            command[-1] = command[-1].replace("https://", "http://", 1)
            return real_popen(command, **kwargs)

        with ThreadingHTTPServer(("127.0.0.1", 0), Handler) as server:
            thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.01})
            thread.start()
            started = time.monotonic()
            try:
                with patch.object(demo_load, "REQUEST_SECONDS", 0.25), \
                     patch.object(demo_load.subprocess, "Popen", side_effect=local_http):
                    if stage:
                        with patch.dict(os.environ, {"REGION": "local", "GITHUB_SHA": "test-commit"}), \
                             patch.object(rollout, "save") as save, patch("builtins.print"):
                            with rollout.demo_traffic(
                                    "demo", {"run_id": "123"}, f"127.0.0.1:{server.server_port}", 10) as check:
                                time.sleep(1.2)
                                check()
                        record = save.call_args.args[1]
                        self.assertEqual(record["submitted"], record["succeeded"])
                        self.assertGreaterEqual(record["submitted"], 10)
                        self.assertLessEqual(record["submitted"], 14)
                        self.assertGreater(record["achieved_rps"], 8)
                        self.assertEqual(record["shutdown"]["outcome"], "drained")
                        self.assertEqual(record["region"], "local")
                        self.assertEqual(record["profile"], "demo")
                        self.assertEqual(record["implementation_commit"], "test-commit")
                        self.assertEqual(record["schema_version"], 2)
                        self.assertEqual(record["generator_state"], "STOPPED")
                        self.assertFalse(any(t.name.startswith("demo-request") for t in threading.enumerate()))
                    elif category is None:
                        demo_load.request(f"127.0.0.1:{server.server_port}", "123")
                    else:
                        with self.assertRaises(demo_load.LoadRequestError) as caught:
                            demo_load.request(f"127.0.0.1:{server.server_port}", "123")
                        self.assertEqual(caught.exception.category, category)
                        self.assertNotIn("private response", str(caught.exception))
                if mode:
                    self.assertLess(time.monotonic() - started, 0.9)
            finally:
                done.set()
                server.shutdown()
                thread.join(2)

    def test_real_curl_stalled_tls_handshake_obeys_total_deadline(self):
        done = threading.Event()

        class Handler(socketserver.BaseRequestHandler):
            def handle(self):
                done.wait(2)

        with socketserver.ThreadingTCPServer(("127.0.0.1", 0), Handler) as server:
            thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.01})
            thread.start()
            started = time.monotonic()
            try:
                with patch.object(demo_load, "REQUEST_SECONDS", 0.25):
                    with self.assertRaises(demo_load.LoadRequestError) as caught:
                        demo_load.request(f"127.0.0.1:{server.server_address[1]}", "123")
                    self.assertEqual(caught.exception.category, "network_timeout")
                self.assertLess(time.monotonic() - started, 0.9)
            finally:
                done.set()
                server.shutdown()
                thread.join(2)


if __name__ == "__main__":
    unittest.main()
