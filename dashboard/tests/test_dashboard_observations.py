import io
import json
import unittest
import zipfile
from types import SimpleNamespace

from dashboard.config import Target
from dashboard.memo import AzureMemo, SharedMemo
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
                                 active_runs=lambda: [self.run] if self.run["status"] != "completed" else [],
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

ACTIVE = (("tenant", "operator-1", frozenset({"ReleaseOperator"})), ("tenant", "viewer-1", frozenset()))


class SharedAzureTests(unittest.TestCase):
    def setUp(self):
        self.now, self.calls = 100.0, []
        test = self

        class Azure:
            def region(self, target, run_id, detail=False, baseline=None):
                test.calls.append((target.app_name, run_id, detail))
                return {"observed_at": "t", "traffic": {f"{target.app_name}--r1": 100},
                        "candidate": None, "candidate_weight": None}

        targets = {r: Target(f"/{r}", f"app-{r}") for r in ("kr", "sea", "usw")}
        store = SimpleNamespace(recent=lambda: [{"operation_id": "o", "kind": "deploy", "actor": "oid", "actor_name": "Kim"}], unresolved=lambda: False, claimed=lambda _: False,
                                by_run=lambda _: None)
        github = SimpleNamespace(runs=lambda *a, **k: [], active_runs=lambda: [])
        self.reader = DashboardReader(SimpleNamespace(repo="owner/repo", repo_id=12), targets, github, None, None,
                                      store, AzureMemo(Azure(), ttl=5, clock=lambda: self.now))

    def test_two_actors_within_window_share_azure_calls_with_own_reasons(self):
        from dashboard.auth import Actor

        operator, viewer = (Actor(*fields) for fields in ACTIVE)
        first = self.reader.snapshot(operator)
        self.now += 4
        second = self.reader.snapshot(viewer)
        self.assertEqual(len(self.calls), 3)
        self.assertEqual(first["regions"]["kr"]["traffic"], {"app-kr--r1": 100})
        self.assertEqual(second["regions"]["sea"]["traffic"], {"app-sea--r1": 100})
        self.assertEqual(first["audit"][0]["actor_name"], "Kim")
        self.assertIsNone(first["actions"]["deploy"])
        self.assertEqual(second["actions"]["deploy"], "ReleaseOperator role required")
        self.now += 2
        self.reader.snapshot(viewer)
        self.assertEqual(len(self.calls), 6)

    def test_concurrent_identical_reads_share_one_call_and_failures_are_not_kept(self):
        import threading
        import time

        release, calls = threading.Event(), []

        def slow():
            calls.append(1)
            release.wait(2)
            return {"value": len(calls)}

        memo = SharedMemo(ttl=5, clock=lambda: self.now)
        results = []
        workers = [threading.Thread(target=lambda: results.append(memo.get("k", slow))) for _ in range(3)]
        for worker in workers:
            worker.start()
        time.sleep(0.05)
        release.set()
        for worker in workers:
            worker.join(2)
        self.assertEqual(calls, [1])
        self.assertEqual(results, [{"value": 1}] * 3)

        def broken():
            calls.append(2)
            raise ValueError("upstream")

        with self.assertRaises(ValueError):
            memo.get("other", broken)
        self.assertEqual(memo.get("other", lambda: "recovered"), "recovered")


class ActiveWorkflowTests(unittest.TestCase):
    def test_one_repository_query_blocks_only_on_allowlisted_active_runs(self):
        from uuid import uuid4

        from dashboard.github import GitHub
        from dashboard.operations import Command, InvalidAction

        runs, calls = [], []

        def send(method, url, headers, body):
            calls.append(url)
            return 200, {}, {"workflow_runs": runs}

        reader = DashboardReader(SimpleNamespace(repo_id=12), {}, GitHub("owner/repo", lambda: "t", send),
                                 None, None, None)
        deploy = Command("deploy", str(uuid4()), profile="demo")
        runs.extend([{"path": ".github/workflows/ci.yml", "status": "in_progress"},
                     {"path": ".github/workflows/release.yml", "status": "completed"}])
        reader.check(deploy)
        self.assertEqual(len(calls), 1)
        for path, status in ((".github/workflows/restore.yml@main", "waiting"),
                             (".github/workflows/release-kr.yml", "queued")):
            runs.append({"path": path, "status": status})
            with self.subTest(path=path), self.assertRaises(InvalidAction):
                reader.check(deploy)
            runs.pop()
        self.assertEqual(len(calls), 3)
        self.assertTrue(calls[0].startswith("https://api.github.com/repos/owner/repo/actions/runs?"))


class AzureTransportTests(unittest.TestCase):
    def test_long_lived_clients_serve_arm_and_concurrent_runtime_probes(self):
        import threading

        import httpx

        from dashboard.observations import AzureReader

        host = "app-kr--r7.blue.koreacentral.azurecontainerapps.io"
        both_probes = threading.Barrier(2, timeout=2)
        arm = {"/kr": {"properties": {"configuration": {"ingress": {"traffic": [
                   {"revisionName": "app-kr--r7", "weight": 100}]}}}},
               "/kr/revisions": {"value": [{"name": "app-kr--r7", "properties": {
                   "active": True, "healthState": "Healthy", "fqdn": host}}]},
               "/kr/revisions/app-kr--r7/replicas": {"value": []}}

        def management(request):
            return httpx.Response(200, json=arm[request.url.path])

        def runtime(request):
            both_probes.wait()
            if request.url.path == "/status":
                return httpx.Response(200, json={"revision": "app-kr--r7", "replica": "p1", "ready": True})
            return httpx.Response(200, json={"revision": "app-kr--r7", "version": "1.2.3", "replica": "p1"})

        reader = AzureReader(SimpleNamespace(get_token=lambda scope: SimpleNamespace(token="t")),
                             http=httpx.Client(transport=httpx.MockTransport(management)),
                             probe=httpx.Client(transport=httpx.MockTransport(runtime)))
        for _ in range(2):
            result = reader.region(Target("/kr", "app-kr"), 7, True)
            self.assertEqual(result["candidate_weight"], 100)
            self.assertEqual(result["revisions"][0]["sample"]["version"], "1.2.3")
        reader.close()
