import base64
import concurrent.futures
import importlib.util
import io
import json
import os
from pathlib import Path
import signal
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from unittest.mock import patch
from http.server import ThreadingHTTPServer

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import common
import inspect_oidc
import metrics
import rollout
import snapshot as snapshot_script

spec = importlib.util.spec_from_file_location("demo_server", ROOT / "app" / "server.py")
server = importlib.util.module_from_spec(spec)
spec.loader.exec_module(server)


def template(release="old", image=None, suffix="old"):
    return {"revisionSuffix": suffix, "scale": {"minReplicas": 1, "maxReplicas": 3},
            "containers": [{"name": "web", "image": image or "demoacr.azurecr.io/safe-demo@sha256:" + "a" * 64,
                            "env": [{"name": "RELEASE_ID", "value": release}]}]}


def snapshot():
    return {"schema": 1, "run_id": "123", "repository": "owner/repo", "region": "kr",
            "app_id": "/subscriptions/sub/resourceGroups/group/providers/Microsoft.App/containerApps/myapp",
            "stable": "myapp--old", "candidate": "myapp--r123", "image": "demoacr.azurecr.io/safe-demo@sha256:" + "b" * 64,
            "traffic": {"myapp--old": 100}, "stable_template": template(), "head_template": template(),
            "stable_template_hash": common.digest(template()), "invariant": "invariant",
            "candidate_probes": common.load(ROOT / "infra" / "health-probes.json")}


def row(revision, source, count=1000, errors=0, p95=40, failures=0):
    return {"Revision": revision, "Source": source, "Samples": count, "Errors": errors,
            "BusinessFailures": failures, "P95": p95}


def rows():
    s = snapshot()
    return [row(rev, source) for rev in (s["stable"], s["candidate"])
            for source in ("organic", "demo", "control")]


class SafetyTests(unittest.TestCase):
    def test_snapshot_validates_public_http_ingress_case_insensitively(self):
        environment = {"GITHUB_RUN_ATTEMPT": "1", "REGION": "kr",
                       "GITHUB_REPOSITORY": "owner/repo", "GITHUB_RUN_ID": "123",
                       "GITHUB_SHA": "commit", "CONTAINER_APP": "myapp",
                       "RESOURCE_GROUP": "group"}
        release = {"repository": "owner/repo", "run_id": "123", "commit": "commit",
                   "image": snapshot()["image"]}
        baseline = {"properties": {"active": True, "healthState": "Healthy",
                                  "provisioningState": "Provisioned", "runningState": "Running",
                                  "template": template()}}
        cases = [(True, transport, True) for transport in
                 ("auto", "Auto", "AUTO", "http", "Http", "HTTP", "http2", "Http2", "HTTP2")]
        cases += [(True, transport, False) for transport in
                  ("tcp", "Tcp", "", None, 123)]
        cases += [(False, "Auto", False), (None, "Auto", False)]
        for external, transport, accepted in cases:
            with self.subTest(external=external, transport=transport):
                ingress = {"external": external, "traffic": [
                    {"revisionName": "myapp--old", "weight": 100}]}
                if transport is not None:
                    ingress["transport"] = transport
                value = {"properties": {"configuration": {
                    "activeRevisionsMode": "Multiple", "ingress": ingress},
                    "template": template()}}
                with patch.dict(os.environ, environment), \
                     patch.object(snapshot_script.Path, "exists", return_value=False), \
                     patch.object(snapshot_script, "load", return_value=release), \
                     patch.object(snapshot_script, "app", return_value=value), \
                     patch.object(snapshot_script, "revision", return_value=baseline), \
                     patch.object(snapshot_script, "az", return_value=[]), \
                     patch.object(snapshot_script, "app_id", return_value=snapshot()["app_id"]), \
                     patch.object(snapshot_script, "save") as save, \
                     patch("builtins.print"):
                    if accepted:
                        snapshot_script.main()
                        save.assert_called_once()
                        self.assertEqual(save.call_args.args[1]["traffic"], {"myapp--old": 100})
                        self.assertEqual(ingress["transport"], transport)
                    else:
                        with self.assertRaisesRegex(RuntimeError, "public HTTP ingress"):
                            snapshot_script.main()
                        save.assert_not_called()

    def test_oidc_inspection_preserves_legacy_and_immutable_subjects(self):
        for subject in ("repo:owner/repo:environment:prod-kr",
                        "repo:owner@123/repo@456:environment:prod-kr"):
            payload = {"sub": subject, "repository_id": "456", "repository_owner_id": "123",
                       "iss": "https://token.actions.githubusercontent.com",
                       "aud": "api://AzureADTokenExchange"}
            encoded = base64.urlsafe_b64encode(json.dumps(payload).encode()).decode().rstrip("=")
            self.assertEqual(inspect_oidc.metadata(f"header.{encoded}.signature"), payload)

    def test_oidc_inspection_does_not_persist_other_token_claims(self):
        payload = {"sub": "actual-sub", "private": "do-not-store", "jti": "do-not-store",
                   "exp": 999, "iat": 100}
        encoded = base64.urlsafe_b64encode(json.dumps(payload).encode()).decode().rstrip("=")
        self.assertEqual(inspect_oidc.metadata(f"header.{encoded}.signature"), {"sub": "actual-sub"})

    def test_identity_helper_uses_inspected_subject_not_legacy_construction(self):
        text = (ROOT / "scripts" / "configure-identity.ps1").read_text()
        self.assertIn("subject = $claims.sub", text)
        self.assertNotIn('subject = "repo:', text)
        self.assertIn("$claims.repository_owner_id", text)

    def test_fresh_oidc_is_requested_for_long_bakes(self):
        with patch.object(common.time, "monotonic", return_value=2000), \
             patch.object(common, "_next_login", 1000), \
             patch.object(common, "refresh_login") as refresh, \
             patch.object(common.subprocess, "run") as process:
            process.return_value.returncode = 0
            process.return_value.stdout = "{}"
            common.az("account", "show")
            common.az("account", "show")
            refresh.assert_called_once()

    def test_oidc_refresh_requires_fresh_github_endpoint(self):
        with patch.dict(os.environ, {"ACTIONS_ID_TOKEN_REQUEST_URL": ""}):
            with self.assertRaisesRegex(RuntimeError, "OIDC endpoint"):
                common.refresh_login()

    def test_oidc_renewal_requests_fresh_assertion_without_printing_it(self):
        assertion = "header.private-payload.signature"
        environment = {"ACTIONS_ID_TOKEN_REQUEST_URL": "https://example.invalid/oidc?x=1",
                       "ACTIONS_ID_TOKEN_REQUEST_TOKEN": "request-secret", "AZURE_CLIENT_ID": "client",
                       "AZURE_TENANT_ID": "tenant", "AZURE_SUBSCRIPTION_ID": "sub"}
        with patch.dict(os.environ, environment), \
             patch.object(common.urllib.request, "urlopen") as endpoint, \
             patch.object(common.subprocess, "run") as process, patch("builtins.print") as output:
            endpoint.return_value.__enter__.return_value = io.StringIO(json.dumps({"value": assertion}))
            process.return_value.returncode = 0
            process.return_value.stderr = ""
            common.refresh_login()
            command = process.call_args_list[0].args[0]
            self.assertEqual(command[command.index("--federated-token") + 1], assertion)
            self.assertIn("audience=api%3A%2F%2FAzureADTokenExchange", endpoint.call_args.args[0].full_url)
            output.assert_not_called()

    def test_oidc_renewal_failure_redacts_assertion(self):
        assertion = "header.private-payload.signature"
        environment = {"ACTIONS_ID_TOKEN_REQUEST_URL": "https://example.invalid/oidc",
                       "ACTIONS_ID_TOKEN_REQUEST_TOKEN": "request-secret", "AZURE_CLIENT_ID": "client",
                       "AZURE_TENANT_ID": "tenant", "AZURE_SUBSCRIPTION_ID": "sub"}
        with patch.dict(os.environ, environment), \
             patch.object(common.urllib.request, "urlopen") as endpoint, \
             patch.object(common.subprocess, "run") as process:
            endpoint.return_value.__enter__.return_value = io.StringIO(json.dumps({"value": assertion}))
            process.return_value.returncode = 1
            process.return_value.stderr = "invalid assertion: " + assertion
            with self.assertRaises(RuntimeError) as error:
                common.refresh_login()
            self.assertNotIn(assertion, str(error.exception))

    def test_oidc_login_timeout_does_not_leak_command_assertion(self):
        assertion = "header.private-payload.signature"
        environment = {"ACTIONS_ID_TOKEN_REQUEST_URL": "https://example.invalid/oidc",
                       "ACTIONS_ID_TOKEN_REQUEST_TOKEN": "request-secret", "AZURE_CLIENT_ID": "client",
                       "AZURE_TENANT_ID": "tenant", "AZURE_SUBSCRIPTION_ID": "sub"}
        with patch.dict(os.environ, environment), \
             patch.object(common.urllib.request, "urlopen") as endpoint, \
             patch.object(common.subprocess, "run",
                          side_effect=common.subprocess.TimeoutExpired(["az", "--federated-token", assertion], 90)):
            endpoint.return_value.__enter__.return_value = io.StringIO(json.dumps({"value": assertion}))
            with self.assertRaises(RuntimeError) as error:
                common.refresh_login()
            self.assertNotIn(assertion, str(error.exception))
            self.assertTrue(error.exception.__suppress_context__)

    def test_rejects_mutable_image(self):
        self.assertFalse(common.image_valid("demoacr.azurecr.io/safe-demo:latest"))
        self.assertTrue(common.image_valid(snapshot()["image"]))

    def test_latest_even_at_zero_is_rejected(self):
        app = {"properties": {"configuration": {"ingress": {"traffic": [
            {"revisionName": "myapp--old", "weight": 100}, {"latestRevision": True, "weight": 0}]}}}}
        with self.assertRaisesRegex(RuntimeError, "latestRevision"):
            common.traffic(app)

    def test_labels_are_rejected(self):
        app = {"properties": {"configuration": {"ingress": {"traffic": [
            {"revisionName": "myapp--old", "label": "live", "weight": 100}]}}}}
        with self.assertRaisesRegex(RuntimeError, "labels"):
            common.traffic(app)

    def test_traffic_totals_and_duplicate_revisions(self):
        for traffic in ([{"revisionName": "old", "weight": 90}],
                        [{"revisionName": "old", "weight": 50}, {"revisionName": "old", "weight": 50}]):
            with self.assertRaises(RuntimeError):
                common.traffic({"properties": {"configuration": {"ingress": {"traffic": traffic}}}})

    def test_zero_entries_can_be_omitted_by_azure(self):
        self.assertTrue(common.equal_traffic({"old": 0, "new": 100}, {"new": 100}))
        self.assertFalse(common.equal_traffic({"old": 100}, {"new": 100}))

    def test_candidate_derives_exact_stable_template(self):
        s = snapshot()
        result = common.expected_candidate(s)
        self.assertEqual(result["revisionSuffix"], "r123")
        self.assertEqual(result["containers"][0]["image"], s["image"])
        self.assertEqual(result["containers"][0]["env"], [{"name": "RELEASE_ID", "value": "123"}])
        self.assertEqual(s["stable_template"], template())

    def test_shared_http_probe_policy_has_explicit_valid_timeouts_and_thresholds(self):
        probes = common.load(ROOT / "infra" / "health-probes.json")
        self.assertEqual([p["type"] for p in probes], ["Startup", "Readiness", "Liveness"])
        for probe in probes:
            self.assertEqual(probe["httpGet"], {"path": "/healthz", "port": 8080, "scheme": "HTTP"})
            self.assertEqual(probe["timeoutSeconds"], 5)
            self.assertGreaterEqual(probe["initialDelaySeconds"], 1)
            self.assertLessEqual(probe["initialDelaySeconds"], 60)
            self.assertGreaterEqual(probe["periodSeconds"], probe["timeoutSeconds"])
            self.assertLessEqual(probe["periodSeconds"], 240)
            self.assertGreaterEqual(probe["failureThreshold"], 1)
            self.assertLessEqual(probe["failureThreshold"], 10)
            self.assertEqual(probe["successThreshold"], 1)
        startup, readiness, liveness = probes
        self.assertEqual(startup["periodSeconds"] * startup["failureThreshold"], 150)
        self.assertEqual((readiness["periodSeconds"], readiness["failureThreshold"]), (5, 3))
        self.assertEqual((liveness["periodSeconds"], liveness["failureThreshold"]), (10, 3))
        self.assertIn("probes: loadJsonContent('./health-probes.json')",
                      (ROOT / "infra" / "region.bicep").read_text())

    def test_candidate_probe_override_preserves_stable_and_legacy_recovery_templates(self):
        s = snapshot()
        old_probes = json.loads(json.dumps(s["candidate_probes"]))
        for probe in old_probes:
            probe["timeoutSeconds"] = 2
        s["stable_template"]["containers"][0]["probes"] = old_probes
        before = json.loads(json.dumps(s))
        candidate = common.expected_candidate(s)
        self.assertEqual(candidate["containers"][0]["probes"],
                         sorted(s["candidate_probes"], key=lambda p: p["type"]))
        self.assertEqual(s, before)
        candidate["containers"][0]["probes"][0]["timeoutSeconds"] = 9
        self.assertEqual(s, before)
        del s["candidate_probes"]
        legacy = common.expected_candidate(s)
        self.assertEqual(legacy["containers"][0]["probes"],
                         sorted(old_probes, key=lambda p: p["type"]))
        self.assertEqual(legacy["containers"][0]["image"], s["image"])

    def test_candidate_probe_policy_is_frozen_and_drift_is_rejected(self):
        s = snapshot()
        candidate = common.expected_candidate(s)
        with patch.object(common, "revision", return_value={
                "name": s["candidate"], "properties": {"template": candidate}}):
            common.candidate_owned(s)
            for probe in candidate["containers"][0]["probes"]:
                with self.subTest(probe=probe["type"]):
                    probe["timeoutSeconds"] = 2
                    with self.assertRaisesRegex(RuntimeError, "Candidate template drift"):
                        common.candidate_owned(s)
                    probe["timeoutSeconds"] = 5
        changed = json.loads(json.dumps(s["candidate_probes"]))
        changed[0]["timeoutSeconds"] = 8
        with patch.object(common, "load", return_value=changed) as read_policy:
            self.assertEqual(common.expected_candidate(s)["containers"][0]["probes"],
                             sorted(s["candidate_probes"], key=lambda p: p["type"]))
            read_policy.assert_not_called()

    def test_snapshot_freezes_candidate_probes_without_modifying_stable(self):
        s = snapshot()
        baseline = template()
        baseline["containers"][0]["probes"] = [{"type": "Readiness", "timeoutSeconds": 2,
                                               "httpGet": {"path": "/healthz", "port": 8080}}]
        value = {"properties": {"configuration": {
            "activeRevisionsMode": "Multiple",
            "ingress": {"external": True, "transport": "auto",
                        "traffic": [{"revisionName": s["stable"], "weight": 100}]}},
            "template": baseline}}
        release = {"repository": s["repository"], "run_id": s["run_id"],
                   "commit": "source-sha", "image": s["image"]}
        environment = {"GITHUB_RUN_ATTEMPT": "1", "GITHUB_RUN_ID": s["run_id"],
                       "GITHUB_REPOSITORY": s["repository"], "GITHUB_SHA": "source-sha",
                       "REGION": s["region"], "CONTAINER_APP": "myapp", "RESOURCE_GROUP": "group"}
        with tempfile.TemporaryDirectory() as directory, \
             patch.dict(os.environ, environment), \
             patch.object(snapshot_script, "Path", side_effect=lambda name: (
                 Path(directory) / name if name == "state" else Path(name))), \
             patch.object(snapshot_script, "load", side_effect=lambda path: (
                 release if path == "release.json" else common.load(path))), \
             patch.object(snapshot_script, "app", return_value=value), \
             patch.object(snapshot_script, "revision", return_value={"properties": {"template": baseline}}), \
             patch.object(snapshot_script, "ready", return_value=True), \
             patch.object(snapshot_script, "app_id", return_value=s["app_id"]), \
             patch.object(snapshot_script, "invariant", return_value="invariant"), \
             patch.object(snapshot_script, "az", return_value=[]), \
             patch.object(snapshot_script, "save") as save, patch("builtins.print"):
            snapshot_script.main()
        captured = save.call_args.args[1]
        self.assertEqual(captured["candidate_probes"], common.load(ROOT / "infra" / "health-probes.json"))
        self.assertEqual(captured["stable_template"], baseline)
        self.assertEqual(captured["stable_template_hash"], common.digest(baseline))
        self.assertEqual(baseline["containers"][0]["probes"][0]["timeoutSeconds"], 2)

    def test_gate_uses_real_business_metrics(self):
        result = metrics.gate(rows(), snapshot(), "production", 10)
        self.assertEqual(result["source"], "organic")
        self.assertIn("stable_live_controls", result)

    def test_demo_scheduler_targets_600_shared_endpoint_requests_per_minute(self):
        elapsed = [0.0]
        stop = unittest.mock.Mock()
        stop.is_set.side_effect = lambda: elapsed[0] >= 60
        stop.wait.side_effect = lambda delay: elapsed.__setitem__(0, elapsed[0] + delay)
        stats = {"submitted": 0, "completed": 0}

        def submit(fn, *args):
            future = concurrent.futures.Future()
            future.set_result(fn(*args))
            return future

        with patch.object(rollout.time, "monotonic", side_effect=lambda: elapsed[0]), \
             patch.object(rollout.concurrent.futures, "ThreadPoolExecutor") as executor, \
             patch.object(rollout.demo_load, "request") as request, patch("builtins.print"):
            executor.return_value.submit.side_effect = submit
            generator = rollout.demo_load.Generator(stop, "shared.example", "123", stats)
            generator.run()
            generator.check()
            self.assertGreaterEqual(stats["completed"], 599)
            self.assertLessEqual(stats["completed"], 601)
            self.assertEqual(stats["submitted"], stats["completed"])
            complete = [interval for interval in stats["intervals"] if interval["complete"]]
            self.assertEqual(len(complete), 1)
            self.assertGreaterEqual(complete[0]["submitted"], 599)
            self.assertLessEqual(complete[0]["submitted"], 601)
            self.assertEqual(stats["queue_delay"]["count"], stats["submitted"])
            self.assertEqual(stats["request_duration"]["count"], stats["submitted"])
            self.assertFalse(generator.pending)
            self.assertTrue(all(call.args == ("shared.example", "123")
                                for call in request.call_args_list))

    def test_demo_load_continues_during_blocking_monitoring_and_stops_on_exit(self):
        with patch.dict(os.environ, {"REGION": "kr"}), \
             patch.object(rollout.demo_load, "request", side_effect=lambda *args: time.sleep(0.15)) as request, \
             patch.object(rollout, "save") as save, patch("builtins.print"):
            with rollout.demo_traffic("demo", snapshot(), "shared.example", 10) as check:
                time.sleep(1.3)  # Simulate a blocking Azure query on the monitoring thread.
                check()
                self.assertGreaterEqual(request.call_count, 10)
                self.assertLessEqual(request.call_count, 16)
            count = request.call_count
            time.sleep(0.2)
            self.assertEqual(request.call_count, count)
            record = save.call_args.args[1]
            self.assertEqual(record["submitted"], record["completed"])
            self.assertGreater(record["achieved_rps"], 7)
            self.assertEqual(record["target_rps"], 10)
            self.assertEqual(record["status"], "completed")
            self.assertEqual(record["shutdown"]["outcome"], "drained")
            self.assertFalse(any(t.name.startswith("demo-request") for t in threading.enumerate()))

    def test_demo_load_surfaces_request_errors_and_records_failure(self):
        attempted = threading.Event()

        def fail_request(*args):
            attempted.set()
            raise RuntimeError("load request failed")

        with patch.dict(os.environ, {"REGION": "kr"}), \
             patch.object(rollout.demo_load, "request", side_effect=fail_request), \
             patch.object(rollout, "save") as save, patch("builtins.print"):
            with self.assertRaisesRegex(RuntimeError, "load request failed"):
                with rollout.demo_traffic("demo", snapshot(), "shared.example", 10):
                    self.assertTrue(attempted.wait(2))
            self.assertEqual(save.call_args.args[1]["status"], "failed")
            self.assertIn("load request failed", save.call_args.args[1]["error"])

    def test_demo_load_stops_and_drains_when_monitoring_fails(self):
        attempted = threading.Event()
        with patch.dict(os.environ, {"REGION": "kr"}), \
             patch.object(rollout.demo_load, "request", side_effect=lambda *args: attempted.set()) as request, \
             patch.object(rollout, "save") as save, patch("builtins.print"):
            with self.assertRaisesRegex(RuntimeError, "monitoring failed"):
                with rollout.demo_traffic("demo", snapshot(), "shared.example", 10):
                    self.assertTrue(attempted.wait(2))
                    raise RuntimeError("monitoring failed")
            count = request.call_count
            time.sleep(0.15)
            self.assertEqual(request.call_count, count)
            self.assertEqual(save.call_args.args[1]["submitted"], save.call_args.args[1]["completed"])

    def test_demo_load_queue_is_bounded_and_capacity_deadline_fails(self):
        elapsed = [0.0]
        stop = unittest.mock.Mock()
        stop.is_set.return_value = False
        stop.wait.side_effect = lambda delay: elapsed.__setitem__(0, elapsed[0] + delay)
        stats = {"submitted": 0, "completed": 0}
        with patch.object(rollout.time, "monotonic", side_effect=lambda: elapsed[0]), \
             patch.object(rollout.time, "sleep", side_effect=stop.wait), \
             patch.object(rollout.concurrent.futures, "ThreadPoolExecutor") as executor:
            pool = executor.return_value
            def submit(*args):
                future = concurrent.futures.Future()
                future.set_running_or_notify_cancel()
                return future
            pool.submit.side_effect = submit
            generator = rollout.demo_load.Generator(stop, "shared.example", "123", stats)
            generator.run()
            with self.assertRaises(rollout.demo_load.LoadCapacityError):
                generator.check()
            self.assertEqual(pool.submit.call_count, 32)
            self.assertAlmostEqual(stats["max_continuous_saturation_seconds"], 30)
            self.assertEqual(stats["unresolved"], 32)
            self.assertEqual(stats["shutdown"]["outcome"], "deadline_exceeded")

    def test_production_never_starts_synthetic_load(self):
        with patch.object(rollout.concurrent.futures, "ThreadPoolExecutor") as executor:
            with rollout.demo_traffic("production", snapshot(), "shared.example", 10) as check:
                check()
            executor.assert_not_called()

    def test_incomplete_telemetry_never_masks_available_slo_failures(self):
        for change in ({"Errors": 100}, {"P95": 999}, {"Samples": -1}):
            data = rows()[1:]
            data[2].update(change)
            with self.subTest(change=change), self.assertRaises(RuntimeError) as error:
                metrics.gate(data, snapshot(), "production", 10)
            self.assertNotIsInstance(error.exception, metrics.IncompleteTelemetry)
        data = rows()
        data[3].update({"Samples": 1, "P95": 200})
        with self.assertRaisesRegex(RuntimeError, "latency regression"):
            metrics.gate(data, snapshot(), "production", 10)

    def test_bake_retries_same_closed_window_with_health_checks_and_bounded_deadline(self):
        for outcome in ("success", "missing", "timeout", "slo", "drift", "health", "query-error"):
            with self.subTest(outcome=outcome):
                elapsed = [0.0]
                now = common.utcnow()
                incomplete = rows()
                incomplete[3]["Samples"] = 14
                bad = rows()
                bad[3]["P95"] = 999
                responses = [incomplete]
                if outcome in ("success", "missing"):
                    responses = [[] if outcome == "missing" else incomplete, rows()]
                elif outcome == "slo":
                    responses = [bad]
                calls = []

                def query(*args):
                    calls.append(args)
                    if outcome == "query-error":
                        raise RuntimeError("query unavailable")
                    return responses[min(len(calls) - 1, len(responses) - 1)], "fixed-query"

                def guard(*args):
                    if outcome == "drift" and elapsed[0] >= 10:
                        raise RuntimeError("drift")
                    return ({"properties": {"configuration": {"ingress": {"fqdn": "app.example"}}}}, {})

                with patch.dict(os.environ, {"REGION": "kr", "LOG_ANALYTICS_WORKSPACE_ID": "workspace"}), \
                     patch.dict(rollout.PROFILES, {"production": {
                         "window": 1, "bake": 1, "lag": 0, "samples": 100, "controls": 30}}), \
                     patch.object(rollout.time, "monotonic", side_effect=lambda: elapsed[0]), \
                     patch.object(rollout.time, "sleep", side_effect=lambda seconds: elapsed.__setitem__(
                         0, elapsed[0] + seconds)), \
                     patch.object(rollout, "utcnow", side_effect=lambda: now + metrics.timedelta(seconds=elapsed[0])), \
                     patch.object(rollout, "revision", return_value={"properties": {"fqdn": "stable.example"}}), \
                     patch.object(rollout, "candidate_owned", return_value={"properties": {"fqdn": "candidate.example"}}), \
                     patch.object(rollout, "ready", side_effect=lambda value:
                                  outcome != "health" or elapsed[0] < 10), \
                     patch.object(rollout, "guarded_state", side_effect=guard) as guarded, \
                     patch.object(rollout, "request") as request, \
                     patch.object(rollout, "query", side_effect=query), \
                     patch.object(rollout, "save") as save, patch("builtins.print"):
                    if outcome in ("success", "missing"):
                        rollout.bake(snapshot(), "production", 10, snapshot()["traffic"], None)
                        self.assertEqual(len(calls), 2)
                        self.assertGreaterEqual(elapsed[0], 31)
                        self.assertGreater(request.call_count, 4)
                        record = save.call_args.args[1]
                        self.assertEqual(len(record["attempts"]), 2)
                    else:
                        with self.assertRaisesRegex(RuntimeError, {
                            "timeout": "Insufficient samples", "slo": "p95 SLO",
                            "drift": "drift", "health": "Candidate health regressed",
                            "query-error": "query unavailable"}[outcome]):
                            rollout.bake(snapshot(), "production", 10, snapshot()["traffic"], None)
                        if outcome == "timeout":
                            self.assertEqual(elapsed[0], 301)
                            self.assertEqual(len(calls), 11)
                            self.assertEqual(save.call_args.args[1]["status"], "failed")
                            self.assertGreater(guarded.call_count, 3)
                        else:
                            self.assertEqual(len(calls), 2 if outcome in ("drift", "health") else 1)
                    self.assertTrue(all(call == calls[0] for call in calls))

    def test_template_normalization_preserves_nondefault_resource_and_scale_drift(self):
        original = template()
        original["containers"][0]["resources"] = {"cpu": 0.25, "memory": "0.5Gi"}
        original["scale"].update({"cooldownPeriod": None, "pollingInterval": None})
        returned = json.loads(json.dumps(original))
        returned["containers"][0]["resources"]["ephemeralStorage"] = "1Gi"
        del returned["scale"]["cooldownPeriod"]
        del returned["scale"]["pollingInterval"]
        self.assertEqual(common.normalized_template(original), common.normalized_template(returned))
        self.assertNotIn("ephemeralStorage", original["containers"][0]["resources"])
        self.assertIn("cooldownPeriod", original["scale"])
        self.assertEqual(returned["containers"][0]["resources"]["ephemeralStorage"], "1Gi")
        for section, key, value in [
            ("resources", "ephemeralStorage", "2Gi"),
            ("resources", "cpu", 0.5),
            ("resources", "memory", "1Gi"),
            ("scale", "cooldownPeriod", 300),
            ("scale", "pollingInterval", 30),
            ("scale", "minReplicas", 2),
        ]:
            changed = json.loads(json.dumps(returned))
            target = changed["containers"][0]["resources"] if section == "resources" else changed["scale"]
            target[key] = value
            with self.subTest(section=section, key=key):
                self.assertNotEqual(common.normalized_template(original), common.normalized_template(changed))

    def test_guard_and_candidate_ownership_accept_only_known_azure_defaults(self):
        s = snapshot()
        s["stable_template"]["containers"][0]["resources"] = {"cpu": 0.25, "memory": "0.5Gi"}
        s["stable_template"]["scale"].update({"cooldownPeriod": None, "pollingInterval": None})
        s["stable_template_hash"] = common.digest(s["stable_template"])
        candidate = common.expected_candidate(s)
        candidate["containers"][0]["resources"]["ephemeralStorage"] = "1Gi"
        candidate["scale"].pop("cooldownPeriod", None)
        candidate["scale"].pop("pollingInterval", None)
        current = {"properties": {"configuration": {"ingress": {"traffic": [
            {"revisionName": s["stable"], "weight": 100}]}}, "template": candidate}}
        environment = {"AZURE_SUBSCRIPTION_ID": "sub", "RESOURCE_GROUP": "group",
                       "CONTAINER_APP": "myapp", "REGION": "kr", "GITHUB_REPOSITORY": "owner/repo"}
        def revision(name):
            return {"name": name,
                    "properties": {"template": s["stable_template"] if name == s["stable"] else candidate}}

        with patch.dict(os.environ, environment), \
             patch.object(common, "app", return_value=current), \
             patch.object(common, "revision", side_effect=revision), \
             patch.object(common, "ready", return_value=True), \
             patch.object(common, "invariant", return_value="invariant"):
            common.guarded_state(s, s["traffic"])
            common.candidate_owned(s)
            candidate["revisionSuffix"] = None
            common.candidate_owned(s)
            candidate["revisionSuffix"] = "another"
            with self.assertRaisesRegex(RuntimeError, "Candidate template drift"):
                common.candidate_owned(s)
            candidate["revisionSuffix"] = "r123"
            candidate["containers"][0]["resources"]["ephemeralStorage"] = "2Gi"
            with self.assertRaisesRegex(RuntimeError, "App template"):
                common.guarded_state(s)
            with self.assertRaisesRegex(RuntimeError, "Candidate template drift"):
                common.candidate_owned(s)

    def test_probe_reordering_is_accepted_but_probe_drift_is_rejected(self):
        s = snapshot()
        candidate = common.expected_candidate(s)
        candidate["containers"][0]["probes"].sort(key=lambda p: p["type"])
        current = {"properties": {"template": candidate}}
        before = json.loads(json.dumps(s))
        with patch.object(common, "verify_context"), \
             patch.object(common, "app", return_value=current), \
             patch.object(common, "invariant", return_value=s["invariant"]), \
             patch.object(common, "traffic", return_value=s["traffic"]), \
             patch.object(common, "ready", return_value=True), \
             patch.object(common, "revision", side_effect=lambda name: {
                 "name": name, "properties": {"template": (
                     s["stable_template"] if name == s["stable"] else candidate)}}):
            common.guarded_state(s, s["traffic"])
            common.candidate_owned(s)
            self.assertEqual(s, before)
            self.assertEqual([p["type"] for p in candidate["containers"][0]["probes"]],
                             ["Liveness", "Readiness", "Startup"])
            for change in ("timeout", "path", "type", "missing", "duplicate"):
                with self.subTest(change=change):
                    probes = candidate["containers"][0]["probes"]
                    original = json.loads(json.dumps(probes))
                    if change == "timeout":
                        probes[0]["timeoutSeconds"] = 2
                    elif change == "path":
                        probes[0]["httpGet"]["path"] = "/work"
                    elif change == "type":
                        probes[0]["type"] = "Startup"
                    elif change == "missing":
                        probes.pop()
                    else:
                        probes.append(json.loads(json.dumps(probes[0])))
                    with self.assertRaisesRegex(RuntimeError, "App template"):
                        common.guarded_state(s, s["traffic"])
                    with self.assertRaisesRegex(RuntimeError, "Candidate template drift"):
                        common.candidate_owned(s)
                    candidate["containers"][0]["probes"] = original

    def test_candidate_null_suffix_requires_exact_revision_name(self):
        s = snapshot()
        candidate = common.expected_candidate(s)
        candidate["revisionSuffix"] = None
        for name in (None, "myapp--another"):
            with self.subTest(name=name), \
                 patch.object(common, "revision", return_value={
                     "name": name, "properties": {"template": candidate}}):
                with self.assertRaisesRegex(RuntimeError, "Candidate revision name mismatch"):
                    common.candidate_owned(s)

    def test_production_rejects_synthetic_only(self):
        with self.assertRaisesRegex(RuntimeError, "Missing"):
            metrics.gate([r for r in rows() if r["Source"] != "organic"], snapshot(), "production", 10)

    def test_missing_stable_or_candidate_fails_closed(self):
        for revision in ("myapp--old", "myapp--r123"):
            with self.subTest(revision=revision), self.assertRaises(RuntimeError):
                metrics.gate([r for r in rows() if r["Revision"] != revision], snapshot(), "production", 50)

    def test_insufficient_sample_and_bad_metrics_fail(self):
        for change in ({"Samples": 1}, {"Errors": 100}, {"BusinessFailures": 100},
                       {"P95": 999}, {"P95": float("nan")}, {"Errors": -1}):
            data = rows()
            data[3].update(change)
            with self.subTest(change=change), self.assertRaises(RuntimeError):
                metrics.gate(data, snapshot(), "production", 10)

    def test_relative_regression_below_absolute_slo_fails(self):
        data = rows()
        data[3]["P95"] = 200
        with self.assertRaisesRegex(RuntimeError, "regression"):
            metrics.gate(data, snapshot(), "production", 50)

    def test_100_percent_requires_reference_and_live_stable_controls(self):
        with self.assertRaisesRegex(RuntimeError, "reference"):
            metrics.gate(rows(), snapshot(), "production", 100)
        reference = {"samples": 1000, "error_rate": 0, "p95": 40}
        organic_candidate = [r for r in rows() if not (r["Revision"] == "myapp--old" and r["Source"] == "organic")]
        self.assertEqual(metrics.gate(organic_candidate, snapshot(), "production", 100, reference)["stable_reference"], reference)
        missing_controls = [r for r in organic_candidate if not (r["Revision"] == "myapp--old" and r["Source"] == "control")]
        with self.assertRaises(RuntimeError):
            metrics.gate(missing_controls, snapshot(), "production", 100, reference)

    def test_kql_has_run_revision_time_and_correct_ingestion_bounds(self):
        start = common.utcnow()
        end = start + metrics.timedelta(minutes=5)
        with patch.object(metrics, "az", return_value=[]) as cli:
            _, kql = metrics.query(snapshot(), start, end, "workspace")
        self.assertIn("RevisionName_s", kql)
        self.assertIn("Run == '123'", kql)
        self.assertIn("arg_max", kql)
        self.assertIn("EventTime >=", kql)
        self.assertIn(common.iso(end + metrics.timedelta(minutes=30)), cli.call_args.args[-1])

    def test_restore_does_not_mutate_on_drift(self):
        with patch.dict(os.environ, {"SOURCE_RUN_ID": "123", "GITHUB_RUN_ID": "999"}), \
             patch.object(rollout, "verify_context"), \
             patch.object(rollout, "guarded_state", side_effect=RuntimeError("drift")), \
             patch.object(rollout, "set_traffic") as write:
            with self.assertRaisesRegex(RuntimeError, "drift"):
                rollout.restore(snapshot())
            write.assert_not_called()

    def test_guard_rejects_another_releases_traffic_or_template(self):
        s = snapshot()
        environment = {"AZURE_SUBSCRIPTION_ID": "sub", "RESOURCE_GROUP": "group",
                       "CONTAINER_APP": "myapp", "REGION": "kr", "GITHUB_REPOSITORY": "owner/repo"}
        for changed_template, changed_traffic in [
            (common.expected_candidate(s), [{"revisionName": "myapp--another", "weight": 100}]),
            (template("another"), [{"revisionName": s["stable"], "weight": 100}]),
        ]:
            current = {"properties": {"configuration": {"ingress": {"traffic": changed_traffic}},
                                      "template": changed_template}}
            with self.subTest(template=changed_template, traffic=changed_traffic), \
                 patch.dict(os.environ, environment), patch.object(common, "app", return_value=current), \
                 patch.object(common, "revision", return_value={"properties": {"template": template()}}), \
                 patch.object(common, "ready", return_value=True), \
                 patch.object(common, "invariant", return_value="invariant"):
                with self.assertRaisesRegex(RuntimeError, "another release|Unknown revision"):
                    common.guarded_state(s)

    def test_restore_only_writes_original_explicit_weights(self):
        s = snapshot()
        app = {"properties": {"configuration": {"ingress": {"fqdn": "app.example"}}}}
        stable = {"properties": {"fqdn": "old.example"}}
        with patch.dict(os.environ, {"SOURCE_RUN_ID": "123", "GITHUB_RUN_ID": "999", "REGION": "kr"}), \
             patch.object(rollout, "verify_context"), \
             patch.object(rollout, "guarded_state", return_value=(app, s["traffic"])), \
             patch.object(rollout, "az", return_value=[]), \
             patch.object(rollout, "cli_app", return_value=[]), \
             patch.object(rollout, "revision", return_value=stable), \
             patch.object(rollout, "request"), patch.object(rollout, "save"), \
             patch.object(rollout, "set_traffic") as write:
            rollout.restore(s)
            write.assert_called_once_with({"myapp--old": 100})

    def test_deploy_rerun_fails_before_mutation(self):
        with patch.dict(os.environ, {"GITHUB_RUN_ATTEMPT": "2"}), patch.object(rollout, "az") as cli:
            with self.assertRaisesRegex(RuntimeError, "reruns"):
                rollout.deploy(snapshot(), "demo")
            cli.assert_not_called()

    def test_deploy_exception_attempts_restore_but_keeps_failed_status(self):
        with patch.dict(os.environ, {"GITHUB_RUN_ATTEMPT": "1", "GITHUB_RUN_ID": "123", "REGION": "kr"}), \
             patch.object(rollout, "verify_context"), patch.object(rollout, "guarded_state"), \
             patch.object(rollout, "baseline_preflight"), \
             patch.object(rollout, "cli_app", return_value=[]), patch.object(rollout, "save"), \
             patch.object(rollout.signal, "signal"), \
             patch.object(rollout, "az", side_effect=[[], RuntimeError("copy failure")]), \
             patch.object(rollout, "restore") as restore:
            with self.assertRaisesRegex(RuntimeError, "copy failure"):
                rollout.deploy(snapshot(), "demo")
            restore.assert_called_once_with(snapshot(), "automatic-rollback")

    def test_candidate_is_copied_from_stable_then_promoted_in_order(self):
        with patch.dict(os.environ, {"GITHUB_RUN_ATTEMPT": "1", "GITHUB_RUN_ID": "123", "REGION": "kr"}), \
             patch.object(rollout, "verify_context"), patch.object(rollout, "guarded_state"), \
             patch.object(rollout, "baseline_preflight") as preflight, \
             patch.object(rollout, "cli_app", return_value=[]), patch.object(rollout, "save") as save, \
             patch.object(rollout, "candidate_owned"), patch.object(rollout, "ready", return_value=True), \
             patch.object(rollout, "wait_ready"), patch.object(rollout, "bake", return_value={"p95": 40}), \
             patch.object(rollout, "az", side_effect=[[], {}]) as cli, \
             patch.object(rollout, "set_traffic") as traffic:
            rollout.deploy(snapshot(), "production")
            preflight.assert_called_once_with(snapshot(), "production")
            self.assertEqual([c.args[0] for c in traffic.call_args_list],
                             [{"myapp--old": 90, "myapp--r123": 10},
                              {"myapp--old": 50, "myapp--r123": 50},
                              {"myapp--old": 0, "myapp--r123": 100}])
            copy_command = cli.call_args.args
            self.assertEqual(copy_command[:2], ("containerapp", "update"))
            self.assertIn("--yaml", copy_command)
            self.assertNotIn("--from-revision", copy_command)
            self.assertNotIn("--image", copy_command)
            payloads = [call.args[1] for call in save.call_args_list
                        if Path(call.args[0]).name == "candidate.json"]
            self.assertEqual(payloads, [{"properties": {"template": common.expected_candidate(snapshot())}}])
            self.assertEqual(payloads[0]["properties"]["template"]["containers"][0]["probes"],
                             sorted(snapshot()["candidate_probes"], key=lambda p: p["type"]))
            self.assertNotIn("latest", copy_command)

    def test_existing_candidate_is_not_blindly_replayed(self):
        with patch.dict(os.environ, {"GITHUB_RUN_ATTEMPT": "1", "GITHUB_RUN_ID": "123", "REGION": "kr"}), \
             patch.object(rollout, "verify_context"), patch.object(rollout, "guarded_state"), \
             patch.object(rollout, "cli_app", return_value=[]), \
             patch.object(rollout, "az", return_value=[{"name": "myapp--r123"}]), \
             patch.object(rollout, "set_traffic") as traffic:
            with self.assertRaisesRegex(RuntimeError, "not safely replayable"):
                rollout.deploy(snapshot(), "demo")
            traffic.assert_not_called()

    def test_candidate_template_file_is_scoped_and_cleaned_up_on_update_failure(self):
        s = snapshot()
        before = json.loads(json.dumps(s))
        files = []

        def persist(path, value):
            if Path(path).name == "candidate.json":
                common.save(path, value)

        def cli(*args):
            if args[:3] == ("containerapp", "revision", "list"):
                return []
            self.assertEqual(args[:2], ("containerapp", "update"))
            filename = Path(args[args.index("--yaml") + 1])
            files.append(filename)
            self.assertEqual(common.load(filename), {
                "properties": {"template": common.expected_candidate(s)}})
            self.assertNotIn("--from-revision", args)
            raise RuntimeError("candidate update failed")

        with patch.dict(os.environ, {"GITHUB_RUN_ATTEMPT": "1", "GITHUB_RUN_ID": "123", "REGION": "kr"}), \
             patch.object(rollout, "verify_context"), patch.object(rollout, "guarded_state"), \
             patch.object(rollout, "baseline_preflight"), patch.object(rollout, "cli_app", return_value=[]), \
             patch.object(rollout, "save", side_effect=persist), patch.object(rollout, "az", side_effect=cli), \
             patch.object(rollout.signal, "signal"), patch.object(rollout, "restore") as restore:
            with self.assertRaisesRegex(RuntimeError, "candidate update failed"):
                rollout.deploy(s, "production")
            restore.assert_called_once_with(s, "automatic-rollback")
        self.assertEqual(len(files), 1)
        self.assertFalse(files[0].exists())
        self.assertFalse(files[0].parent.exists())
        self.assertEqual(s, before)

    def test_durable_baseline_upload_precedes_rollout(self):
        text = (ROOT / ".github" / "workflows" / "region.yml").read_text()
        self.assertLess(text.index("id: baseline-upload"), text.index("id: rollout"))
        self.assertIn("if-no-files-found: error", text)
        self.assertIn('if test -f "evidence/$REGION/mutation-intent.json"', text)

    def test_baseline_contract_failure_never_creates_candidate_or_writes_traffic(self):
        environment = {"GITHUB_RUN_ATTEMPT": "1", "GITHUB_RUN_ID": "123", "REGION": "kr"}
        with patch.dict(os.environ, environment), \
             patch.object(rollout, "verify_context"), patch.object(rollout, "guarded_state"), \
             patch.object(rollout, "cli_app", return_value=[]), \
             patch.object(rollout, "az", return_value=[]) as cli, \
             patch.object(rollout, "revision", return_value={"properties": {"fqdn": "stable.example"}}), \
             patch.object(rollout, "request", side_effect=[{}, RuntimeError("Business contract failed")]), \
             patch.object(rollout, "query") as query, patch.object(rollout, "save") as save, \
             patch.object(rollout, "set_traffic") as traffic, patch.object(rollout, "restore") as restore:
            with self.assertRaisesRegex(RuntimeError, "Business contract"):
                rollout.deploy(snapshot(), "production")
            self.assertEqual(cli.call_count, 1)
            self.assertEqual(cli.call_args.args[:3], ("containerapp", "revision", "list"))
            traffic.assert_not_called()
            restore.assert_not_called()
            query.assert_not_called()
            self.assertEqual([call.args[0].name for call in save.call_args_list], ["baseline-preflight.json"])
            self.assertEqual(save.call_args.args[1]["status"], "failed")

    def test_baseline_organic_sample_or_slo_failure_never_mutates_azure(self):
        environment = {"GITHUB_RUN_ATTEMPT": "1", "GITHUB_RUN_ID": "123", "REGION": "kr",
                       "LOG_ANALYTICS_WORKSPACE_ID": "workspace"}
        for organic in (row("myapp--old", "organic", count=1),
                        row("myapp--old", "organic", p95=600),
                        row("myapp--old", "organic", failures=100)):
            with self.subTest(organic=organic), patch.dict(os.environ, environment), \
                 patch.object(rollout, "verify_context"), patch.object(rollout, "guarded_state"), \
                 patch.object(rollout, "cli_app", return_value=[]), \
                 patch.object(rollout, "az", return_value=[]) as cli, \
                 patch.object(rollout, "revision", return_value={"properties": {"fqdn": "stable.example"}}), \
                 patch.object(rollout, "request"), \
                 patch.object(rollout, "query", return_value=([organic], "baseline-query")), \
                 patch.object(rollout, "save") as save, patch.object(rollout, "set_traffic") as traffic, \
                 patch.object(rollout, "restore") as restore:
                with self.assertRaises(RuntimeError):
                    rollout.deploy(snapshot(), "production")
                self.assertEqual(cli.call_count, 1)
                self.assertEqual(cli.call_args.args[:3], ("containerapp", "revision", "list"))
                traffic.assert_not_called()
                restore.assert_not_called()
                self.assertEqual([call.args[0].name for call in save.call_args_list], ["baseline-preflight.json"])
                self.assertEqual(save.call_args.args[1]["status"], "failed")

    def test_production_preflight_uses_recent_closed_stable_organic_window(self):
        now = common.utcnow()
        with patch.dict(os.environ, {"REGION": "kr", "LOG_ANALYTICS_WORKSPACE_ID": "workspace"}), \
             patch.object(rollout, "utcnow", return_value=now), \
             patch.object(rollout, "guarded_state"), \
             patch.object(rollout, "revision", return_value={"properties": {"fqdn": "stable.example"}}), \
             patch.object(rollout, "request") as request, \
             patch.object(rollout, "query", return_value=([row("myapp--old", "organic")], "kql")) as query, \
             patch.object(rollout, "save") as save:
            rollout.baseline_preflight(snapshot(), "production")
            end = now - metrics.timedelta(seconds=120)
            query.assert_called_once_with(snapshot(), end - metrics.timedelta(seconds=300), end, "workspace")
            self.assertEqual([call.args[2] for call in request.call_args_list], ["probe", "probe"])
            self.assertEqual(save.call_args.args[1]["organic_baseline"]["samples"], 1000)
            self.assertEqual(save.call_args.args[1]["status"], "passed")

    def test_demo_preflight_only_uses_direct_probes_and_records_limitation(self):
        with patch.dict(os.environ, {"REGION": "kr"}), \
             patch.object(rollout, "guarded_state"), \
             patch.object(rollout, "revision", return_value={"properties": {"fqdn": "stable.example"}}), \
             patch.object(rollout, "request") as request, patch.object(rollout, "query") as query, \
             patch.object(rollout, "save") as save:
            rollout.baseline_preflight(snapshot(), "demo")
            query.assert_not_called()
            self.assertEqual([call.args[1] for call in request.call_args_list], ["/healthz", "/work"])
            self.assertEqual([call.args[2] for call in request.call_args_list], ["probe", "probe"])
            self.assertIn("not organic baseline SLO", save.call_args.args[1]["limitation"])

    def test_health_endpoint_remains_responsive_during_slow_business_request(self):
        work_started = threading.Event()

        class ObservedHandler(server.Handler):
            def do_GET(self):
                if self.path == "/work":
                    work_started.set()
                super().do_GET()

        with patch.dict(os.environ, {"DEMO_LATENCY_MS": "2500", "DEMO_FAILURE_PERCENT": "0"}), \
             patch("builtins.print"), ThreadingHTTPServer(("127.0.0.1", 0), ObservedHandler) as httpd:
            thread = threading.Thread(target=httpd.serve_forever)
            thread.start()
            endpoint = f"http://127.0.0.1:{httpd.server_port}"
            try:
                with concurrent.futures.ThreadPoolExecutor(max_workers=1) as client:
                    work = client.submit(urllib.request.urlopen, f"{endpoint}/work", timeout=5)
                    self.assertTrue(work_started.wait(2))
                    started = time.monotonic()
                    with urllib.request.urlopen(f"{endpoint}/healthz", timeout=1) as response:
                        self.assertEqual(response.status, 200)
                        self.assertTrue(json.load(response)["ready"])
                    self.assertLess(time.monotonic() - started, 1)
                    with work.result(timeout=5) as response:
                        self.assertEqual(json.load(response)["result"], 42)
            finally:
                httpd.shutdown()
                thread.join(2)

    def test_signal_enters_error_path(self):
        with self.assertRaisesRegex(RuntimeError, "rollback"):
            rollout.cancelled(signal.SIGTERM, None)

    def test_region_workflows_are_sequential_and_share_restore_lock(self):
        release = (ROOT / ".github" / "workflows" / "release.yml").read_text()
        restore = (ROOT / ".github" / "workflows" / "restore.yml").read_text()
        for text in (release, restore):
            self.assertIn("group: safe-aca-production", text)
            self.assertIn("cancel-in-progress: false", text)
        self.assertIn("needs: korea", release)
        self.assertIn("needs: southeastasia", release)
        self.assertNotIn("matrix:", release)

    def test_business_function(self):
        self.assertTrue(server.business_result(0))
        self.assertFalse(server.business_result(100))

    def test_http_contract_and_request_log(self):
        with patch.dict(os.environ, {"DEMO_FAILURE_PERCENT": "0", "DEMO_LATENCY_MS": "0",
                                     "CONTAINER_APP_REVISION": "test--revision"}):
            httpd = ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
            thread = threading.Thread(target=httpd.serve_forever, daemon=True)
            thread.start()
            try:
                with patch("builtins.print") as log:
                    with urllib.request.urlopen(f"http://127.0.0.1:{httpd.server_port}/healthz") as response:
                        self.assertTrue(json.load(response)["ready"])
                    log.assert_not_called()
                    req = urllib.request.Request(f"http://127.0.0.1:{httpd.server_port}/work",
                                                 headers={"X-Safe-Source": "control", "X-Safe-Run": "123"})
                    with urllib.request.urlopen(req) as response:
                        result = json.load(response)
                    self.assertEqual(result["result"], 42)
                    # The response can complete before the server's finally block.
                    thread_event = threading.Event()
                    for _ in range(100):
                        if log.called:
                            break
                        thread_event.wait(0.01)
                    record = json.loads(log.call_args.args[0])
                    self.assertEqual(record["source"], "control")
                    self.assertEqual(record["monitor_run"], "123")
                    self.assertEqual(record["revision"], "test--revision")
            finally:
                httpd.shutdown()
                httpd.server_close()
                thread.join()


if __name__ == "__main__":
    unittest.main()
