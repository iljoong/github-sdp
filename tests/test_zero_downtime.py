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


if __name__ == "__main__":
    unittest.main()
