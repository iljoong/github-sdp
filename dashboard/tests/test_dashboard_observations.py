import io
import json
import re
import unittest
import zipfile
from datetime import datetime, timezone
from types import SimpleNamespace
from uuid import uuid4

from dashboard.config import Target
from dashboard.github import GitHub, ReadCache, RemoteError
from dashboard.memo import AzureMemo, SharedMemo
from dashboard.evidence import Evidence, UnsafeArtifact, read_member
from dashboard.observations import (DashboardReader, candidate, job_states, previous_revision, runtime_sample,
                                    safe_env)
from dashboard.operations import Command, InvalidAction
from pathlib import Path

TRANSLATIONS = (Path(__file__).resolve().parents[1] / "static" / "dashboard.js").read_text(encoding="utf-8")

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


class RerunRecoveryTests(unittest.TestCase):
    """Recovery of a release that GitHub re-ran: evidence and baseline still come from attempt 1."""

    @staticmethod
    def rerun(jobs_by_attempt=None, attempt=2):
        fixture = EvidenceDisplay("completed")
        fixture.run["run_attempt"] = attempt
        fixture.jobs_read = []

        def jobs(run_id, number):
            fixture.jobs_read.append(number)
            return (jobs_by_attempt or {}).get(number, [])

        fixture.reader.github.jobs = jobs
        return fixture

    def test_rollback_is_eligible_for_a_completed_rerun_with_a_verified_baseline(self):
        fixture = self.rerun()
        self.assertIsNone(fixture.snapshot()["actions"]["rollback"])
        fixture.reader.check(SimpleNamespace(action="rollback", source_run_id=7, region="kr"))

    def test_retire_is_refused_when_a_later_attempt_started_the_region_rollout(self):
        fixture = self.rerun({2: [{"name": "korea / rollout", "run_attempt": 2, "status": "completed",
                                   "conclusion": "failure", "started_at": "2026-11-22T10:00:00Z",
                                   "steps": [{"name": "Shift 10%", "status": "completed", "conclusion": "failure"}]}]})
        reason = "A later attempt of this release ran the region's rollout"
        self.assertEqual(fixture.snapshot()["actions"]["retire"], reason)
        with self.assertRaisesRegex(InvalidAction, reason):
            fixture.reader.check(SimpleNamespace(action="retire", source_run_id=7, region="kr"))

    def test_retire_stays_eligible_when_later_attempts_never_started_the_region_rollout(self):
        fixture = self.rerun({2: [{"name": "korea / rollout", "run_attempt": 1, "status": "completed",
                                   "conclusion": "success", "steps": [{"name": "Shift 10%"}]},
                                  {"name": "westus / rollout", "run_attempt": 2, "status": "completed",
                                   "conclusion": "failure", "steps": [{"name": "Shift 10%"}]}],
                              3: [{"name": "korea / rollout", "run_attempt": 3, "status": "completed",
                                   "conclusion": "cancelled", "steps": []}]}, attempt=3)
        self.assertIsNone(fixture.snapshot()["actions"]["retire"])
        fixture.reader.check(SimpleNamespace(action="retire", source_run_id=7, region="kr"))

    def test_first_attempt_retire_reads_no_later_attempts(self):
        fixture = self.rerun(attempt=1)
        self.assertIsNone(fixture.snapshot()["actions"]["retire"])
        self.assertNotIn(2, fixture.jobs_read)


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


T0 = datetime(2026, 11, 22, 10, 0, tzinfo=timezone.utc).timestamp()


def iso(seconds):
    return datetime.fromtimestamp(seconds, timezone.utc).isoformat()


def release_run(run_id, status, workflow="release.yml"):
    return {"id": run_id, "path": f".github/workflows/{workflow}", "head_branch": "main", "event": "workflow_dispatch",
            "repository": {"id": 12}, "run_attempt": 1, "status": status, "head_sha": SHA, "created_at": iso(T0),
            "conclusion": "success" if status == "completed" else None}


class Console:
    """Snapshot reader over offline fakes: GitHub through the real ReadCache/GitHub client, one injected clock."""

    def __init__(self, status="in_progress"):
        self.now, self.github_status = T0, 200
        self.runs = {7: release_run(7, status)}
        self.jobs, self.rules = {}, {}
        self.ops = [{"operation_id": "o7", "kind": "deploy", "status": "accepted", "run_id": 7, "actor": "requester",
                     "created_at": iso(T0)}]
        self.weights, self.azure_age, self.sample_age, self.azure_down = {}, {}, 0, set()
        self.audit_down = False
        self.claims = set()
        self.cache = ReadCache(self._send, clock=lambda: self.now)
        # The console has been following change hints for an hour and has not seen one yet.
        self.hints = SimpleNamespace(started_at=T0 - 3600, last_hint_at=None)

        def recent():
            if self.audit_down:
                raise ValueError("table unavailable")
            return self.ops

        store = SimpleNamespace(recent=recent, unresolved=lambda: False, claimed=lambda claim: claim in self.claims,
                                by_run=lambda run_id: next((op for op in self.ops if op.get("run_id") == run_id), None))
        self.reader = DashboardReader(
            SimpleNamespace(repo="owner/repo", repo_id=12), TARGETS, GitHub("owner/repo", lambda: "t", self.cache),
            SimpleNamespace(file=lambda *_, **__: None), SimpleNamespace(rules=lambda run_id: self.rules.get(run_id, [])),
            store, SimpleNamespace(region=self._azure), cache=self.cache, clock=lambda: self.now, hints=self.hints)

    def _send(self, method, url, headers, body):
        if self.github_status != 200:
            return self.github_status, {"retry-after": "60"} if self.github_status == 429 else {}, None
        path = url.split("/actions", 1)[1]
        if path.startswith("/workflows/release.yml/runs?"):
            return 200, {}, {"workflow_runs": [run for run in self.runs.values() if run["path"].endswith("release.yml")]}
        if path.startswith("/runs?"):
            return 200, {}, {"workflow_runs": [run for run in self.runs.values() if run["status"] != "completed"]}
        match = re.fullmatch(r"/runs/(\d+)(/attempts/(\d+)/jobs\?.*)?", path)
        run_id = int(match.group(1))
        if not match.group(2):
            return 200, {}, self.runs[run_id]
        jobs = self.jobs.get(run_id, [])
        # Jobs keyed by attempt model GitHub re-runs; a plain list is attempt 1.
        return 200, {}, {"jobs": jobs.get(int(match.group(3)), []) if isinstance(jobs, dict) else
                         jobs if match.group(3) == "1" else []}

    def _azure(self, target, run_id, detail, baseline):
        region = target.app_name.split("-", 1)[1]
        if region in self.azure_down:
            raise ValueError("ARM unavailable")
        desired = candidate(target, run_id) if run_id else None
        weight = self.weights.get(region, 0)
        traffic = {f"{target.app_name}--r5": 100 - weight, **({desired: weight} if desired else {})}
        result = {"observed_at": iso(self.now - self.azure_age.get(region, 0)), "traffic": traffic,
                  "candidate": desired, "candidate_weight": weight if desired else None}
        if detail:
            result["revisions"] = [
                {"name": f"{target.app_name}--r5", "active": True, "health": "Healthy", "sample": None},
                {"name": desired, "active": True, "health": "Healthy",
                 "sample": {"replica": "p1", "observed_at": iso(self.now - self.sample_age)}}]
        return result

    def snapshot(self, run_id=None, region="kr", actor=None):
        return self.reader.snapshot(actor or SimpleNamespace(roles={"ReleaseApprover"}, object_id="approver"),
                                    run_id, region)


class SnapshotFreshnessTests(unittest.TestCase):
    def test_github_source_reports_age_of_cached_reads_and_rate_limit_back_off(self):
        console = Console()
        self.assertEqual(console.snapshot()["sources"]["github"],
                         {"observed_at": iso(T0), "age_seconds": 0, "state": "fresh"})
        console.now += 4
        self.assertEqual(console.snapshot()["sources"]["github"],
                         {"observed_at": iso(T0), "age_seconds": 4, "state": "fresh"})
        console.now += 2
        console.github_status = 429
        snapshot = console.snapshot()
        self.assertFalse(snapshot["github_available"])
        self.assertEqual(snapshot["sources"]["github"],
                         {"observed_at": None, "age_seconds": None, "state": "unavailable", "reason": "rate_limited",
                          "retry_after_seconds": 60})

    def test_github_upstream_failure_is_reported_without_back_off(self):
        console = Console()
        console.github_status = 502
        self.assertEqual(console.snapshot()["sources"]["github"],
                         {"observed_at": None, "age_seconds": None, "state": "unavailable", "reason": "upstream_error"})

    def test_azure_runtime_and_audit_sources_report_fresh_stale_and_unavailable(self):
        console = Console()
        console.azure_age = {"kr": 3, "sea": 20}
        console.azure_down = {"usw"}
        console.sample_age = 16
        sources = console.snapshot(region="kr")["sources"]
        self.assertEqual(sources["azure:kr"], {"observed_at": iso(T0 - 3), "age_seconds": 3, "state": "fresh"})
        self.assertEqual(sources["azure:sea"], {"observed_at": iso(T0 - 20), "age_seconds": 20, "state": "stale"})
        self.assertEqual(sources["azure:usw"], {"observed_at": None, "age_seconds": None, "state": "unavailable",
                                                "reason": "upstream_error"})
        self.assertEqual(sources["runtime:app-kr--r7"], {"observed_at": iso(T0 - 16), "age_seconds": 16, "state": "stale"})
        self.assertEqual(sources["runtime:app-kr--r5"], {"observed_at": None, "age_seconds": None,
                                                         "state": "unavailable", "reason": "not_produced"})
        self.assertEqual(sources["audit"], {"observed_at": iso(T0), "age_seconds": 0, "state": "fresh"})
        self.assertNotIn("runtime:app-sea--r7", sources)
        console.audit_down = True
        self.assertEqual(console.snapshot()["sources"]["audit"]["state"], "unavailable")

    def test_hints_are_stale_when_an_active_run_has_produced_no_change_hint_for_too_long(self):
        console = Console("in_progress")
        console.now = T0 + 901
        self.assertEqual(console.snapshot()["sources"]["hints"],
                         {"observed_at": None, "age_seconds": None, "state": "stale", "reason": "no_hints"})
        console.hints.last_hint_at = T0 - 100
        self.assertEqual(console.snapshot()["sources"]["hints"],
                         {"observed_at": iso(T0 - 100), "age_seconds": 1001, "state": "stale", "reason": "no_hints"})

    def test_hints_are_fresh_after_a_recent_hint(self):
        console = Console("in_progress")
        console.now = T0 + 2000
        console.hints.last_hint_at = T0 + 1970
        self.assertEqual(console.snapshot()["sources"]["hints"],
                         {"observed_at": iso(T0 + 1970), "age_seconds": 30, "state": "fresh"})

    def test_hints_are_not_stale_without_an_active_run(self):
        console = Console("completed")
        console.now = T0 + 5000
        self.assertEqual(console.snapshot()["sources"]["hints"],
                         {"observed_at": None, "age_seconds": None, "state": "fresh"})

    def test_a_run_or_console_that_started_recently_is_given_time_for_its_first_hint(self):
        console = Console("in_progress")
        console.now = T0 + 600
        self.assertEqual(console.snapshot()["sources"]["hints"]["state"], "fresh")
        console.now = T0 + 5000
        console.hints.started_at = T0 + 4500
        self.assertEqual(console.snapshot()["sources"]["hints"]["state"], "fresh")


class ApprovalInboxTests(unittest.TestCase):
    def test_inbox_lists_gates_of_every_active_run_and_region_regardless_of_selection(self):
        console = Console("completed")
        console.runs[9] = release_run(9, "waiting")
        console.jobs[9] = [{"name": "southeastasia / rollout", "status": "waiting"}]
        console.rules[9] = [{"environment": "prod-sea", "sha": SHA}]
        console.ops.append({"operation_id": "o9", "kind": "deploy", "status": "accepted", "run_id": 9,
                            "actor": "requester", "created_at": iso(T0)})
        snapshot = console.snapshot(run_id=7, region="kr")
        self.assertEqual(snapshot["pending"], [])
        self.assertEqual(snapshot["inbox"], [{"operation_id": "o9", "run_id": 9, "kind": "deploy", "region": "sea",
                                              "environment": "prod-sea", "reason": None, "source_run_id": None,
                                              "run_attempt": 1, "rerun": False, "reject_reason": None}])
        requester = SimpleNamespace(roles={"ReleaseApprover"}, object_id="requester")
        self.assertEqual(console.snapshot(run_id=7, region="usw", actor=requester)["inbox"][0]["reason"],
                         "A different Entra approver is required")


    @staticmethod
    def waiting_console_run(console, run_id):
        console.runs[run_id] = release_run(run_id, "waiting")
        console.jobs[run_id] = [{"name": "southeastasia / rollout", "status": "waiting"}]
        console.rules[run_id] = [{"environment": "prod-sea", "sha": SHA}]
        console.ops.append({"operation_id": f"o{run_id}", "kind": "deploy", "status": "accepted", "run_id": run_id,
                            "actor": "requester", "created_at": iso(T0)})

    def test_runs_not_started_from_the_console_do_not_crowd_out_console_gates(self):
        console = Console("completed")
        for run_id in range(100, 110):
            console.runs[run_id] = release_run(run_id, "in_progress")
        self.waiting_console_run(console, 9)
        snapshot = console.snapshot(run_id=7)
        self.assertEqual([gate["run_id"] for gate in snapshot["inbox"]], [9])
        self.assertFalse(snapshot["inbox_truncated"])

    def test_inbox_reports_when_more_console_runs_are_active_than_it_lists(self):
        console = Console("completed")
        for run_id in range(20, 31):
            self.waiting_console_run(console, run_id)
        snapshot = console.snapshot(run_id=7)
        self.assertEqual(len(snapshot["inbox"]), 10)
        self.assertTrue(snapshot["inbox_truncated"])


class RerunGateTests(unittest.TestCase):
    """A GitHub re-run (attempt > 1) of a console release raises a new protection rule request."""

    @staticmethod
    def rerun(console, run_id=9):
        console.runs[run_id] = {**release_run(run_id, "waiting"), "run_attempt": 2, "run_started_at": iso(T0 - 600)}
        console.jobs[run_id] = {1: [{"name": "westus / rollout", "status": "completed", "conclusion": "failure"}],
                                2: [{"name": "westus / rollout", "status": "waiting"}]}
        console.rules[run_id] = [{"environment": "prod-usw", "sha": SHA, "received_at": iso(T0 - 3000)},
                                 {"environment": "prod-usw", "sha": SHA, "received_at": iso(T0 - 300)}]
        console.ops.append({"operation_id": f"o{run_id}", "kind": "deploy", "status": "accepted", "run_id": run_id,
                            "actor": "requester", "created_at": iso(T0 - 4000)})

    def test_rerun_gate_is_listed_reject_only(self):
        console = Console("completed")
        self.rerun(console)
        self.assertEqual(console.snapshot(run_id=7)["inbox"], [
            {"operation_id": "o9", "run_id": 9, "kind": "deploy", "region": "usw", "environment": "prod-usw",
             "reason": "Re-run blocked — reject only", "source_run_id": None, "run_attempt": 2, "rerun": True,
             "reject_reason": None}])

    def test_rerun_gate_can_be_rejected_only_when_nothing_else_blocks_the_decision(self):
        console = Console("completed")
        self.rerun(console)
        requester = SimpleNamespace(roles={"ReleaseApprover"}, object_id="requester")
        gate = console.snapshot(run_id=7, actor=requester)["inbox"][0]
        self.assertEqual((gate["rerun"], gate["reason"], gate["reject_reason"]),
                         (True, "A different Entra approver is required", "A different Entra approver is required"))

    def test_rule_request_received_before_the_current_attempt_started_is_ignored(self):
        console = Console("completed")
        self.rerun(console)
        console.rules[9] = console.rules[9][:1]
        self.assertEqual(console.snapshot(run_id=7)["inbox"], [])

    def test_attempt_one_decision_does_not_hide_the_attempt_two_gate(self):
        console = Console("completed")
        self.rerun(console)
        console.claims.add("review:9:prod-usw")
        self.assertEqual([gate["run_attempt"] for gate in console.snapshot(run_id=7)["inbox"]], [2])
        console.claims.add("review:9:prod-usw:2")
        console.now += 10
        self.assertEqual(console.snapshot(run_id=7)["inbox"], [])

    def test_timeline_shows_approval_requests_and_decisions_per_attempt(self):
        console = Console("completed")
        self.rerun(console)
        console.ops += [
            {"operation_id": "d1", "kind": "approve", "status": "accepted", "source_run_id": 9,
             "environment": "prod-usw", "actor": "a2", "created_at": iso(T0 - 2900)},
            {"operation_id": "d2", "kind": "reject", "status": "accepted", "source_run_id": 9,
             "environment": "prod-usw", "run_attempt": 2, "actor": "a2", "created_at": iso(T0 - 200)}]
        timeline = console.snapshot(run_id=9)["timeline"]
        self.assertEqual([(e["kind"], e["attempt"]) for e in timeline if e["environment"] == "prod-usw"], [
            ("approval_requested", 1), ("approve", 1), ("approval_requested", 2), ("reject", 2)])

    def test_selected_rerun_release_shows_its_attempt_and_rejects_approval_in_pending(self):
        console = Console("completed")
        self.rerun(console)
        snapshot = console.snapshot(run_id=9, region="usw")
        self.assertEqual(snapshot["run"]["run_attempt"], 2)
        self.assertEqual(snapshot["history"][-1]["run_attempt"], 2)
        self.assertEqual([(g["environment"], g["reason"], g["rerun"]) for g in snapshot["pending"]],
                         [("prod-usw", "Re-run blocked — reject only", True)])


class RegionProgressTests(unittest.TestCase):
    def test_regions_report_current_step_elapsed_failed_step_log_and_traffic_stage(self):
        console = Console()
        log = "https://github.com/owner/repo/actions/runs/7/job/{}"
        console.jobs[7] = [
            {"name": "korea / rollout", "status": "in_progress", "html_url": log.format(71), "steps": [
                {"name": "Shift 10%", "status": "completed", "conclusion": "success",
                 "started_at": "2026-11-22T09:55:00Z"},
                {"name": "Bake at 50%", "status": "in_progress", "conclusion": None,
                 "started_at": "2026-11-22T09:58:30Z"}]},
            {"name": "southeastasia / rollout", "status": "completed", "conclusion": "failure",
             "html_url": log.format(72), "steps": [
                 {"name": "Shift 10%", "status": "completed", "conclusion": "success"},
                 {"name": "Verify health", "status": "completed", "conclusion": "failure"}]},
            {"name": "westus / rollout", "status": "queued", "html_url": "https://evil.example/owner/repo/actions/runs/7/job/73",
             "steps": []}]
        console.weights = {"kr": 50, "sea": 30, "usw": 0}
        regions = console.snapshot()["regions"]
        self.assertEqual(regions["kr"]["progress"], {"step": "Bake at 50%", "started_at": iso(T0 - 90),
                                                     "elapsed_seconds": 90, "failed_step": None,
                                                     "log_url": log.format(71)})
        self.assertEqual(regions["sea"]["progress"], {"step": None, "started_at": None, "elapsed_seconds": None,
                                                      "failed_step": "Verify health", "log_url": log.format(72)})
        self.assertEqual(regions["usw"]["progress"], {"step": None, "started_at": None, "elapsed_seconds": None,
                                                      "failed_step": None, "log_url": None})
        self.assertEqual({r: regions[r]["traffic_stage"] for r in regions}, {"kr": 50, "sea": 10, "usw": 0})
        self.assertEqual(regions["kr"]["job"], "in_progress")

    def test_regions_name_their_container_app_for_the_resource_tree(self):
        console = Console()
        console.azure_down = {"usw"}
        regions = console.snapshot(region="sea")["regions"]
        self.assertEqual({r: regions[r]["app"] for r in regions}, {"kr": "app-kr", "sea": "app-sea", "usw": "app-usw"})

    def test_traffic_stage_is_unknown_without_azure_or_a_release(self):
        console = Console()
        console.azure_down = {"kr"}
        console.weights = {"sea": 100}
        regions = console.snapshot()["regions"]
        self.assertIsNone(regions["kr"]["traffic_stage"])
        self.assertEqual(regions["sea"]["traffic_stage"], 100)
        console.runs.clear()
        console.now += 10
        self.assertIsNone(console.snapshot()["regions"]["sea"]["traffic_stage"])


OPERATOR_ACTOR = SimpleNamespace(roles={"ReleaseOperator"}, object_id="operator")


class ReleaseHistoryTests(unittest.TestCase):
    def test_history_lists_start_profile_commit_requester_duration_and_conclusion(self):
        console = Console()
        console.runs[7].update(run_started_at=iso(T0 - 60), updated_at=iso(T0 - 5))
        console.runs[6] = {**release_run(6, "completed"), "head_sha": "d" * 40, "conclusion": "failure",
                           "run_started_at": iso(T0 - 3600), "updated_at": iso(T0 - 3000)}
        console.ops = [{"operation_id": "o7", "kind": "deploy", "status": "accepted", "run_id": 7, "actor": "a7",
                        "actor_name": "Kim", "profile": "demo", "created_at": iso(T0 - 61)},
                       {"operation_id": "o6", "kind": "deploy", "status": "accepted", "run_id": 6, "actor": "a6",
                        "profile": "production", "created_at": iso(T0 - 3601)}]
        history = console.snapshot(run_id=7)["history"]
        self.assertEqual([{k: entry[k] for k in ("id", "status", "conclusion", "started_at", "profile", "commit",
                                                 "requester", "duration_seconds", "url")} for entry in history], [
            {"id": 7, "status": "in_progress", "conclusion": None, "started_at": iso(T0 - 60), "profile": "demo",
             "commit": SHA, "requester": "Kim", "duration_seconds": 60,
             "url": "https://github.com/owner/repo/actions/runs/7"},
            {"id": 6, "status": "completed", "conclusion": "failure", "started_at": iso(T0 - 3600),
             "profile": "production", "commit": "d" * 40, "requester": None, "duration_seconds": 600,
             "url": "https://github.com/owner/repo/actions/runs/6"}])

    def test_releases_are_named_by_their_run_display_title_or_workflow_name(self):
        console = Console()
        console.runs[7].update(display_title="Release 2026.11.22 · checkout fix", name="release")
        console.runs[6] = {**release_run(6, "completed"), "name": "release"}
        console.runs[5] = release_run(5, "completed")
        snapshot = console.snapshot(run_id=7)
        self.assertEqual(snapshot["run"]["display_title"], "Release 2026.11.22 · checkout fix")
        expected = {7: "Release 2026.11.22 · checkout fix", 6: "release", 5: None}
        self.assertEqual({row["id"]: row["display_title"] for row in snapshot["runs"]}, expected)
        self.assertEqual({row["id"]: row["display_title"] for row in snapshot["history"]}, expected)

    def test_history_entries_carry_rollback_and_retire_reasons(self):
        console = Console("completed")
        console.runs[6] = release_run(6, "completed")
        console.runs[5] = {**release_run(5, "completed"), "run_attempt": 2}
        history = {entry["id"]: entry["actions"] for entry in console.snapshot(run_id=7, actor=OPERATOR_ACTOR)["history"]}
        unverified = "The deployment baseline is unavailable, expired or unverified"
        self.assertEqual(history[7], {"rollback": unverified, "retire": unverified})
        select = "Select this release to verify its recovery evidence"
        self.assertEqual(history[6], {"rollback": select, "retire": select})
        self.assertEqual(history[5], {"rollback": select, "retire": select})
        viewer = console.snapshot(run_id=7)["history"]
        self.assertEqual(viewer[1]["actions"]["rollback"], "ReleaseOperator role required")


class EventTimelineTests(unittest.TestCase):
    def test_timeline_merges_jobs_approval_requests_decisions_and_audit_in_time_order(self):
        console = Console()
        console.jobs[7] = [
            {"name": "build / build", "status": "completed", "conclusion": "success",
             "started_at": iso(T0 - 290), "completed_at": iso(T0 - 200)},
            {"name": "korea / rollout", "status": "in_progress", "conclusion": None,
             "started_at": iso(T0 - 50), "completed_at": None}]
        console.rules[7] = [{"environment": "prod-kr", "sha": SHA, "received_at": iso(T0 - 150)}]
        console.ops = [
            {"operation_id": "o7", "kind": "deploy", "status": "accepted", "run_id": 7, "actor": "a1",
             "actor_name": "Kim", "created_at": iso(T0 - 300)},
            {"operation_id": "d1", "kind": "approve", "status": "accepted", "source_run_id": 7, "environment": "prod-kr",
             "actor": "a2", "actor_name": "Lee", "created_at": iso(T0 - 100)},
            {"operation_id": "x", "kind": "cancel", "status": "accepted", "source_run_id": 99, "actor": "a3",
             "created_at": iso(T0 - 10)}]
        timeline = console.snapshot(run_id=7)["timeline"]
        self.assertEqual([(e["at"], e["kind"], e["name"], e["status"], e["region"], e["actor_name"]) for e in timeline], [
            (iso(T0 - 300), "deploy", None, "accepted", None, "Kim"),
            (iso(T0 - 290), "job_started", "build / build", None, None, None),
            (iso(T0 - 200), "job_completed", "build / build", "success", None, None),
            (iso(T0 - 150), "approval_requested", None, None, "kr", None),
            (iso(T0 - 100), "approve", None, "accepted", "kr", "Lee"),
            (iso(T0 - 50), "job_started", "korea / rollout", None, "kr", None)])
        self.assertEqual(timeline[4]["environment"], "prod-kr")
        self.assertEqual(timeline[4]["actor"], "a2")

    def test_timeline_keeps_only_the_newest_entries(self):
        console = Console()
        console.ops = []
        console.jobs[7] = [{"name": f"step {n}", "status": "completed", "conclusion": "success",
                            "started_at": iso(T0 - 1000 + 2 * n), "completed_at": iso(T0 - 999 + 2 * n)}
                           for n in range(40)]
        timeline = console.snapshot(run_id=7)["timeline"]
        self.assertEqual(len(timeline), 50)
        self.assertEqual(timeline[0]["at"], iso(T0 - 1000 + 30))
        self.assertEqual(timeline[-1]["at"], iso(T0 - 999 + 78))


class ResumeTests(unittest.TestCase):
    """Resume from the first unfinished region: snapshot reasons and the uncached write-time check."""

    REASONS = {
        "conclusion": "Only a failed or cancelled release can be resumed",
        "done": "Every region already succeeded",
        "earlier": "An earlier region did not succeed in the latest attempt",
        "build": "The source release did not build an image",
        "traffic": "The start region's traffic is not on exactly one healthy revision at 100%",
        "foreign": "Current traffic belongs to a different release",
    }

    @staticmethod
    def failed_at(region, conclusion="failure"):
        console = Console("completed")
        console.runs[7]["conclusion"] = conclusion
        order = ("kr", "sea", "usw")
        names = {"kr": "korea", "sea": "southeastasia", "usw": "westus"}
        console.jobs[7] = [{"name": "build / build", "status": "completed", "conclusion": "success"}, *[
            {"name": f"{names[r]} / rollout", "status": "completed",
             "conclusion": "success" if n < order.index(region) else conclusion if r == region else "skipped"}
            for n, r in enumerate(order)]]
        return console

    @staticmethod
    def check(console):
        return console.reader.check(Command("resume", str(uuid4()), source_run_id=7))

    def refused(self, console, reason, actor=OPERATOR_ACTOR):
        self.assertEqual(console.snapshot(run_id=7, actor=actor)["actions"]["resume"], reason)
        with self.assertRaisesRegex(InvalidAction, re.escape(reason)):
            self.check(console)
        # Every refusal is a static string the console translates.
        self.assertIn(f'\n  "{reason}": ', TRANSLATIONS)

    def test_resume_starts_at_the_first_unfinished_region_with_the_source_profile(self):
        for region in ("kr", "sea", "usw"):
            console = self.failed_at(region)
            console.ops[0]["profile"] = "demo"
            self.assertEqual(self.check(console), {"start_region": region, "profile": "demo"})
            snapshot = console.snapshot(run_id=7, actor=OPERATOR_ACTOR)
            self.assertIsNone(snapshot["actions"]["resume"])
            self.assertEqual(snapshot["resume_start"], region)
        cancelled = self.failed_at("usw", "cancelled")
        cancelled.ops = []
        self.assertEqual(self.check(cancelled), {"start_region": "usw", "profile": "production"})

    def test_start_region_on_the_source_candidate_or_verified_stable_is_eligible(self):
        console = self.failed_at("usw")
        console.weights = {"usw": 100}
        self.assertEqual(self.check(console)["start_region"], "usw")
        console.weights = {}
        console.reader.evidence = SimpleNamespace(file=lambda *_, **__: {
            "run_id": "7", "repository": "owner/repo", "region": "usw", "commit": SHA, "app_id": "/sample-usw",
            "candidate": "app-usw--r7", "stable": "app-usw--r5", "traffic": {"app-usw--r5": 100}})
        self.assertIsNone(console.snapshot(run_id=7, actor=OPERATOR_ACTOR)["actions"]["resume"])
        self.assertEqual(self.check(console)["start_region"], "usw")

    def test_resume_is_refused_unless_the_source_failed_or_was_cancelled(self):
        console = self.failed_at("usw")
        console.runs[7]["conclusion"] = "success"
        self.refused(console, self.REASONS["conclusion"])

    def test_resume_is_refused_while_the_source_is_running(self):
        console = self.failed_at("usw")
        console.runs[7].update(status="in_progress", conclusion=None)
        self.assertEqual(console.snapshot(run_id=7, actor=OPERATOR_ACTOR)["actions"]["resume"],
                         "A protected workflow is still active")
        with self.assertRaisesRegex(InvalidAction, "Another protected workflow has not completed"):
            self.check(console)

    def test_resume_is_refused_when_every_region_succeeded(self):
        console = self.failed_at("usw")
        console.jobs[7][-1]["conclusion"] = "success"
        self.refused(console, self.REASONS["done"])

    def test_resume_is_refused_when_an_earlier_region_did_not_succeed(self):
        console = self.failed_at("kr")
        console.jobs[7][2]["conclusion"] = "success"
        self.refused(console, self.REASONS["earlier"])

    def test_resume_is_refused_without_a_built_image(self):
        console = self.failed_at("kr")
        console.jobs[7][0]["conclusion"] = "failure"
        self.refused(console, self.REASONS["build"])

    @staticmethod
    def resumed_from(start, failed):
        """Source 7 as a resume run that started at `start` and failed at `failed`."""
        console = ResumeTests.failed_at(failed)
        order = ("kr", "sea", "usw")
        names = {"kr": "korea", "sea": "southeastasia", "usw": "westus"}
        console.jobs[7] = [{"name": "build", "status": "completed", "conclusion": "skipped"},
                           {"name": "promote", "status": "completed", "conclusion": "success"}, *[
            {"name": names[r], "status": "completed", "conclusion": "skipped"} if n < order.index(start) else
            {"name": f"{names[r]} / rollout", "status": "completed",
             "conclusion": "success" if n < order.index(failed) else "failure" if r == failed else "skipped"}
            for n, r in enumerate(order)]]
        console.ops[0].update(source_run_id=3, start_region=start, profile="demo")
        return console

    def test_a_resume_run_can_be_resumed_past_the_regions_it_skipped(self):
        for start, failed in (("sea", "sea"), ("sea", "usw"), ("usw", "usw"), ("kr", "sea")):
            with self.subTest(start=start, failed=failed):
                console = self.resumed_from(start, failed)
                self.assertEqual(self.check(console), {"start_region": failed, "profile": "demo"})
                snapshot = console.snapshot(run_id=7, actor=OPERATOR_ACTOR)
                self.assertIsNone(snapshot["actions"]["resume"])
                self.assertEqual(snapshot["resume_start"], failed)

    def test_skipped_regions_are_satisfied_only_before_the_first_region_a_successful_resume_ran(self):
        built = self.failed_at("usw")
        built.jobs[7][1] = {"name": "korea", "status": "completed", "conclusion": "skipped"}
        self.refused(built, self.REASONS["earlier"])
        promote_failed = self.resumed_from("sea", "sea")
        promote_failed.jobs[7][1]["conclusion"] = "failure"
        self.refused(promote_failed, self.REASONS["build"])
        nothing_ran = self.resumed_from("sea", "sea")
        nothing_ran.jobs[7][3] = {"name": "southeastasia", "status": "completed", "conclusion": "skipped"}
        self.refused(nothing_ran, "The source release did not deploy any region")

    def test_resume_is_refused_unless_the_start_region_serves_one_healthy_revision_at_100(self):
        console = self.failed_at("usw")
        console.weights = {"usw": 50}
        self.refused(console, self.REASONS["traffic"])
        console.weights = {}
        original = console._azure

        def unhealthy(*args):
            result = original(*args)
            for row in result.get("revisions", []):
                row["health"] = "Unhealthy"
            return result

        console.reader.azure = SimpleNamespace(region=unhealthy)
        self.refused(console, self.REASONS["traffic"])

    def test_resume_is_refused_when_start_region_traffic_is_neither_baseline_nor_candidate(self):
        console = self.failed_at("usw")
        console.reader.evidence = SimpleNamespace(file=lambda *_, **__: {
            "run_id": "7", "repository": "owner/repo", "region": "usw", "commit": SHA, "app_id": "/sample-usw",
            "candidate": "app-usw--r7", "stable": "app-usw--r4", "traffic": {"app-usw--r4": 100}})
        self.refused(console, self.REASONS["foreign"])

    def test_resume_requires_operator_role_no_unresolved_write_and_no_active_run(self):
        console = self.failed_at("usw")
        self.assertEqual(console.snapshot(run_id=7)["actions"]["resume"], "ReleaseOperator role required")
        console.reader.store.unresolved = lambda: True
        self.assertEqual(console.snapshot(run_id=7, actor=OPERATOR_ACTOR)["actions"]["resume"],
                         "A write is unresolved or GitHub state is unavailable")
        console.reader.store.unresolved = lambda: False
        console.runs[8] = release_run(8, "in_progress")
        console.cache.invalidate(8)
        self.assertEqual(console.snapshot(run_id=7, actor=OPERATOR_ACTOR)["actions"]["resume"],
                         "A protected workflow is still active")
        with self.assertRaisesRegex(InvalidAction, "Another protected workflow has not completed"):
            self.check(console)

    def test_resume_run_is_a_release_linked_to_its_source_and_its_gates_reach_the_inbox(self):
        console = self.failed_at("usw")
        console.runs[9] = release_run(9, "waiting")
        console.jobs[9] = [{"name": "build / build", "status": "completed", "conclusion": "skipped"},
                           {"name": "westus / rollout", "status": "waiting"}]
        console.rules[9] = [{"environment": "prod-usw", "sha": SHA}]
        console.ops.append({"operation_id": "o9", "kind": "deploy", "status": "accepted", "run_id": 9,
                            "source_run_id": 7, "start_region": "usw", "profile": "production",
                            "actor": "requester", "created_at": iso(T0)})
        snapshot = console.snapshot(run_id=7)
        self.assertEqual(snapshot["inbox"], [{"operation_id": "o9", "run_id": 9, "kind": "deploy", "region": "usw",
                                              "environment": "prod-usw", "reason": None, "source_run_id": 7,
                                              "run_attempt": 1, "rerun": False, "reject_reason": None}])
        self.assertEqual({entry["id"]: entry["resumed_from"] for entry in snapshot["history"]}, {7: None, 9: 7})
        self.assertEqual(console.snapshot(run_id=9)["pending"], snapshot["inbox"])


class ActiveWorkflowTests(unittest.TestCase):
    """Write-time active-workflow check over a fake GitHub that honours `status=` and pagination."""

    def setUp(self):
        from uuid import uuid4

        from dashboard.operations import Command

        self.runs, self.calls, self.failing = [], [], None

        def send(method, url, headers, body):
            self.calls.append(url)
            query = dict(part.split("=", 1) for part in url.split("?", 1)[1].split("&"))
            if query.get("status") == self.failing:
                return 502, {}, None
            matching = [run for run in self.runs if run["status"] == query.get("status")]
            page, size = int(query["page"]), int(query["per_page"])
            return 200, {}, {"workflow_runs": matching[(page - 1) * size:page * size]}

        self.reader = DashboardReader(SimpleNamespace(repo_id=12), {}, GitHub("owner/repo", lambda: "t", send),
                                      None, None, None)
        self.deploy = Command("deploy", str(uuid4()), profile="demo")

    def add(self, count, path, status):
        self.runs.extend({"id": len(self.runs) + 1, "path": path, "status": status} for _ in range(count))

    def test_unfinished_allowlisted_runs_block_and_others_do_not(self):
        self.add(1, ".github/workflows/ci.yml", "in_progress")
        self.add(1, ".github/workflows/release.yml", "completed")
        self.reader.check(self.deploy)
        for path, status in ((".github/workflows/restore.yml@main", "waiting"),
                             (".github/workflows/release-kr.yml", "queued"),
                             (".github/workflows/demo-retire.yml", "requested"),
                             (".github/workflows/release.yml", "pending")):
            self.runs.append({"id": 99, "path": path, "status": status})
            with self.subTest(path=path, status=status), self.assertRaises(InvalidAction):
                self.reader.check(self.deploy)
            self.runs.pop()
        self.assertTrue(all(url.startswith("https://api.github.com/repos/owner/repo/actions/runs?branch=main&"
                                           "event=workflow_dispatch&status=") for url in self.calls))
        self.assertNotIn("status=completed", " ".join(self.calls))

    def test_an_older_protected_run_beyond_the_first_page_still_blocks(self):
        self.add(100, ".github/workflows/ci.yml", "in_progress")
        self.add(1, ".github/workflows/release.yml", "in_progress")
        with self.assertRaises(InvalidAction):
            self.reader.check(self.deploy)
        self.assertEqual(sum("status=in_progress" in url for url in self.calls), 2)

    def test_any_failed_status_query_fails_the_write_check_closed(self):
        self.failing = "waiting"
        with self.assertRaises(RemoteError):
            self.reader.check(self.deploy)


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
