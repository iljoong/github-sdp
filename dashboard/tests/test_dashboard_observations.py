import io
import json
import unittest
import zipfile
from types import SimpleNamespace

from dashboard.config import Target
from dashboard.evidence import Evidence, UnsafeArtifact, read_member
from dashboard.observations import (DashboardReader, candidate, job_states, previous_revision, runtime_sample,
                                    safe_env)


def archive(entries):
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as z:
        for key, value in entries.items():
            z.writestr(key, json.dumps(value))
    return buffer.getvalue()


SHA = "c" * 40
TARGETS = {r: Target(f"/sample-{r}", f"app-{r}") for r in ("kr", "sea", "usw")}
STABLE, CANDIDATE = "app-kr--r5", "app-kr--r7"
ARTIFACTS = {
    7: [{"id": 70, "name": "baseline-7-kr", "expired": False},
        {"id": 71, "name": "evidence-7-kr-1", "expired": False}],
    8: [{"id": 80, "name": "restore-evidence-8-kr-1", "expired": False}],
}
BLOBS = {
    70: {"baseline-kr.json": {"run_id": "7", "repository": "owner/repo", "region": "kr", "commit": SHA,
                              "app_id": "/sample-kr", "candidate": CANDIDATE, "stable": STABLE,
                              "traffic": {STABLE: 100}}},
    71: {"kr/success.json": {"revision": CANDIDATE, "profile": "demo"}},
    80: {"kr/rollback.json": {"verified": True, "source_run": "7", "stable": STABLE}},
}


class EvidenceDisplay:
    """Reader wired to offline fakes; counts artifact downloads through the injected downloader."""

    def __init__(self, status):
        self.downloads = []
        self.run = {"id": 7, "path": ".github/workflows/release.yml", "head_branch": "main", "event": "workflow_dispatch",
                    "repository": {"id": 12}, "run_attempt": 1, "status": status, "head_sha": SHA,
                    "conclusion": "success" if status == "completed" else None}
        github = SimpleNamespace(runs=lambda *_, **__: [self.run], run=lambda _: self.run, jobs=lambda *_: [],
                                 artifacts=lambda run_id: ARTIFACTS.get(run_id, []),
                                 artifact_location=lambda artifact_id: f"https://blob/{artifact_id}")
        ops = [{"operation_id": "o1", "kind": "rollback", "status": "completed", "source_run_id": 7, "run_id": 8,
                "region": "kr", "actor": "a", "created_at": "t"}]
        store = SimpleNamespace(recent=lambda: ops, unresolved=lambda: False, claimed=lambda _: False,
                                by_run=lambda _: None)
        evidence = Evidence(github, self._download)
        self.reader = DashboardReader(SimpleNamespace(repo="owner/repo", repo_id=12), TARGETS, github, evidence,
                                      SimpleNamespace(rules=lambda _: []), store, SimpleNamespace(region=self._azure))

    def _download(self, url):
        artifact_id = int(url.rsplit("/", 1)[-1])
        self.downloads.append(artifact_id)
        return archive(BLOBS[artifact_id])

    @staticmethod
    def _azure(target, run_id, detail, baseline):
        result = {"observed_at": "t", "traffic": {CANDIDATE: 100}, "candidate": CANDIDATE, "candidate_weight": 100}
        if detail:
            result["baseline"] = baseline["stable"] if baseline else None
            result["revisions"] = [{"name": STABLE, "active": True, "health": "Healthy"},
                                   {"name": CANDIDATE, "active": True, "health": "Healthy"}]
        return result

    def snapshot(self):
        return self.reader.snapshot(SimpleNamespace(roles={"ReleaseOperator"}, object_id="x"), 7, "kr")


class EvidenceCachingTests(unittest.TestCase):
    def test_in_progress_snapshot_downloads_no_artifacts(self):
        fixture = EvidenceDisplay("in_progress")
        fixture.snapshot()
        self.assertEqual(fixture.downloads, [])

    def test_previous_revision_label_falls_back_to_azure_traffic(self):
        self.assertEqual(previous_revision({"app--r5": 90, "app--r7": 10}, "app--r7"), "app--r5")
        self.assertEqual(previous_revision({"app--r3": 0, "app--r5": 50, "app--r7": 50}, "app--r7"), "app--r5")
        self.assertIsNone(previous_revision({"app--r5": 0, "app--r7": 100}, "app--r7"))

    def test_completed_run_downloads_each_artifact_once_across_snapshots(self):
        fixture = EvidenceDisplay("completed")
        first = fixture.snapshot()
        second = fixture.snapshot()
        self.assertEqual(sorted(fixture.downloads), [70, 71, 80])
        self.assertEqual(first["actions"], second["actions"])
        self.assertIsNone(second["actions"]["retire"])

    def test_write_time_check_downloads_fresh_evidence_even_when_cached(self):
        fixture = EvidenceDisplay("completed")
        fixture.snapshot()
        fixture.downloads.clear()
        fixture.reader.check(SimpleNamespace(action="retire", source_run_id=7, region="kr"))
        self.assertEqual(sorted(fixture.downloads), [70, 71])


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
                                 SimpleNamespace(file=lambda *_, **__: artifact), None, None)
        self.assertEqual(reader.baseline({"id":52,"head_sha":"c"}, "kr"),
                         {"stable":"test-app--r49","candidate":"test-app--r52"})
        artifact["traffic"]["test-app--r40"] = 5
        self.assertIsNone(reader.baseline({"id":52,"head_sha":"c"}, "kr"))
