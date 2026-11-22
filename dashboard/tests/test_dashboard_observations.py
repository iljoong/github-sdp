import io
import json
import unittest
import zipfile
from types import SimpleNamespace

from dashboard.config import Target
from dashboard.evidence import Evidence, UnsafeArtifact, read_member
from dashboard.observations import DashboardReader, candidate, job_states, runtime_sample, safe_env


def archive(entries):
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as z:
        for key, value in entries.items():
            z.writestr(key, json.dumps(value))
    return buffer.getvalue()


class ObservationTests(unittest.TestCase):
    def test_waiting_job_not_completed_just_because_traffic_at_100(self):
        jobs = [{"name": "build / build", "status": "completed", "conclusion": "success"},
                {"name": "korea / rollout", "status": "waiting", "conclusion": None}]
        result = job_states(jobs, "deploy")
        self.assertEqual(result["kr"], "waiting")
        self.assertEqual(result["build"], "success")
        self.assertEqual(result["sea"], "not_started")
        self.assertEqual(candidate(Target("ignored", "sample-kr"), 421), "sample-kr--r421")

    def test_runtime_requires_matching_revision_and_filters_fields(self):
        fields = {"revision": "stable", "replica": "p1", "ready": True, "inflight": [{}], "secret": "hidden"}
        self.assertIsNone(runtime_sample(fields, "candidate"))
        sample = runtime_sample(fields, "stable")
        self.assertEqual(sample["inflight_count"], 1)
        self.assertNotIn("secret", sample)
        self.assertEqual(safe_env([{"name":"GRACE_SECONDS", "value":"30"}, {"name":"PASSWORD","value":"hidden"},
                                   {"name":"RELEASE_ID","secretRef":"hidden"}]), {"GRACE_SECONDS":"30"})

    def test_expired_and_wrong_attempt_artifacts_are_not_used(self):
        listing = [{"id": 3, "name":"evidence-99-kr-1", "expired":False},
                   {"id": 4, "name":"evidence-99-kr-2", "expired":True}]
        ev = Evidence(SimpleNamespace(artifacts=lambda _:listing), lambda _: self.fail("unexpected download"))
        self.assertIsNone(ev.file(99, "evidence-99-kr-2", "kr/success.json"))
        self.assertIsNone(ev.file(99, "evidence-99-kr-3", "kr/success.json"))

    def test_artifact_member_is_bounded_and_no_path_escape(self):
        self.assertEqual(read_member(archive({"kr/success.json": {"revision":"test"}}), "kr/success.json"),
                         {"revision":"test"})
        with self.assertRaises(UnsafeArtifact):
            read_member(archive({"../outside.json":{}, "kr/success.json":{}}), "kr/success.json")

    def test_remote_gate_requires_exact_run_attempt_sha_and_environment(self):
        sha = "a" * 40
        run = {"id":7,"path":".github/workflows/release.yml@main","head_branch":"main","repository":{"id":12},
               "event":"workflow_dispatch","run_attempt":1,"status":"waiting","head_sha":sha}
        reader = DashboardReader(SimpleNamespace(repo_id=12), None, SimpleNamespace(
            run=lambda _:run, jobs=lambda *_:[{"name":"korea / rollout","status":"waiting"}]),
            None, SimpleNamespace(rules=lambda _:[{"environment":"prod-kr","sha":sha}]), None)
        op = {"run_id":7,"kind":"deploy"}
        self.assertTrue(reader.gate(op, "prod-kr"))
        self.assertFalse(reader.gate(op, "prod-sea"))
        run["run_attempt"] = 2
        self.assertFalse(reader.gate(op, "prod-kr"))

    def test_baseline_accepts_zero_weight_for_older_revision(self):
        target = Target("/sample", "test-app")
        artifact = {"run_id":"52", "repository":"owner/repo", "region":"kr", "commit":"c",
                    "app_id":"/sample", "candidate":"test-app--r52", "stable":"test-app--r49",
                    "traffic":{"test-app--r49":100, "test-app--r40":0}}
        reader = DashboardReader(SimpleNamespace(repo="owner/repo"), {"kr": target}, None,
                                 SimpleNamespace(file=lambda *_: artifact), None, None)
        self.assertEqual(reader.baseline({"id":52,"head_sha":"c"}, "kr"),
                         {"stable":"test-app--r49","candidate":"test-app--r52"})
        artifact["traffic"]["test-app--r40"] = 5
        self.assertIsNone(reader.baseline({"id":52,"head_sha":"c"}, "kr"))
