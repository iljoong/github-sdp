import base64
import importlib.util
import io
import json
import os
from pathlib import Path
import signal
import sys
import threading
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
            "stable_template_hash": common.digest(template()), "invariant": "invariant"}


def row(revision, source, count=1000, errors=0, p95=40, failures=0):
    return {"Revision": revision, "Source": source, "Samples": count, "Errors": errors,
            "BusinessFailures": failures, "P95": p95}


def rows():
    s = snapshot()
    return [row(rev, source) for rev in (s["stable"], s["candidate"])
            for source in ("organic", "demo", "control")]


class SafetyTests(unittest.TestCase):
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

    def test_gate_uses_real_business_metrics(self):
        result = metrics.gate(rows(), snapshot(), "production", 10)
        self.assertEqual(result["source"], "organic")
        self.assertIn("stable_live_controls", result)

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
             patch.object(rollout, "cli_app", return_value=[]), patch.object(rollout, "save"), \
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
            self.assertIn("--from-revision", copy_command)
            self.assertEqual(copy_command[copy_command.index("--from-revision") + 1], "myapp--old")
            self.assertEqual(copy_command[copy_command.index("--image") + 1], snapshot()["image"])
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
        self.assertIn("needs: germany", release)
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
