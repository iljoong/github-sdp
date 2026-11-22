import base64
import json
import unittest
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit
from xml.etree import ElementTree

from fastapi.testclient import TestClient

from dashboard.auth import APPROVER, Actor, OPERATOR
from dashboard.github import RemoteError
from dashboard.server import create_app
from test_dashboard_auth_github import TENANT, USER, principal


class DashboardHTTPTests(unittest.TestCase):
    def setUp(self):
        app = create_app()
        app.state.settings = SimpleNamespace(tenant_id=TENANT, origin="https://console.example.test")
        app.state.verify_session = lambda token: Actor(TENANT, USER, frozenset({OPERATOR})) if token == "valid-fixture" else None
        app.state.reader = SimpleNamespace(snapshot=lambda actor, run_id, region: {"run": run_id, "region": region})
        app.state.operations = SimpleNamespace(request=lambda actor, command: {"status": "accepted"})
        app.state.changes = SimpleNamespace(poll=lambda: 0)
        self.client = TestClient(app, base_url="https://console.example.test")
        self.headers = {"x-ms-client-principal": principal("ReleaseOperator"), "x-ms-token-aad-id-token": "valid-fixture"}

    def test_ui_and_data_require_an_assigned_user(self):
        self.assertEqual(self.client.get("/").status_code, 403)
        self.assertEqual(self.client.get("/api/dashboard").status_code, 403)
        principal_only = {"x-ms-client-principal": principal("ReleaseOperator")}
        self.assertEqual(self.client.get("/api/dashboard", headers=principal_only).status_code, 403)
        mismatch = {**self.headers, "x-ms-client-principal": principal("ReleaseApprover")}
        self.assertEqual(self.client.get("/api/dashboard", headers=mismatch).status_code, 403)
        response = self.client.get("/", headers=self.headers)
        self.assertEqual(response.status_code, 200)
        self.assertIn("Build", response.text)
        self.assertNotIn("모의 진행", response.text)
        data = self.client.get("/api/dashboard?run_id=34&region=sea", headers=self.headers)
        self.assertEqual(data.json(), {"run":34, "region":"sea"})

    def test_snapshot_response_includes_freshness_inbox_and_region_progress(self):
        from test_dashboard_observations import Console

        self.client.app.state.reader = Console().reader
        data = self.client.get("/api/dashboard?region=sea", headers=self.headers).json()
        self.assertEqual(set(data["sources"]), {"github", "audit", "azure:kr", "azure:sea", "azure:usw",
                                                "runtime:app-sea--r5", "runtime:app-sea--r7"})
        self.assertEqual(set(data["sources"]["github"]), {"observed_at", "age_seconds", "state"})
        self.assertEqual(data["sources"]["runtime:app-sea--r5"]["reason"], "not_produced")
        self.assertEqual(data["inbox"], [])
        self.assertEqual(set(data["regions"]["kr"]["progress"]),
                         {"step", "started_at", "elapsed_seconds", "failed_step", "log_url"})
        self.assertEqual(data["regions"]["kr"]["traffic_stage"], 0)
        self.assertTrue({"run", "runs", "regions", "build", "pending", "actions", "audit", "observed_at",
                         "github_available", "audit_available"} <= set(data))

    def test_writes_need_origin_and_reject_extra_targets(self):
        command = {"action":"deploy","request_id":"83468040-d438-4007-bf2d-8068282a966d","profile":"demo"}
        self.assertEqual(self.client.post("/api/operations", json=command, headers=self.headers).status_code, 403)
        headers = {**self.headers, "origin":"https://console.example.test","x-dashboard-write":"1"}
        self.assertEqual(self.client.post("/api/operations", json=command, headers=headers).status_code, 200)
        self.assertEqual(self.client.post("/api/operations", json={**command,"repo":"other/repo"}, headers=headers).status_code, 422)

    def test_banner_uses_verified_account_name_and_all_dashboard_roles(self):
        headers = {**self.headers, "x-ms-client-principal": principal(OPERATOR, APPROVER)}
        self.client.app.state.verify_session = lambda _: SimpleNamespace()
        self.assertEqual(self.client.get("/", headers=headers).status_code, 403)
        self.client.app.state.verify_session = lambda _: Actor(
            TENANT, USER, frozenset({OPERATOR, APPROVER}), display_name="Test <Admin>")
        response = self.client.get("/", headers=headers)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers["cache-control"], "no-store")
        banner = ElementTree.fromstring("<header>" + response.text.split("<header>", 1)[1].split("</header>", 1)[0] + "</header>")
        self.assertEqual(banner.find(".//*[@id='account-name']").text, "Test <Admin>")
        self.assertCountEqual([item.text for item in banner.findall(".//span[@class='account-role']")],
                              [OPERATOR, APPROVER])

    def test_switch_account_returns_from_easy_auth_logout_to_entra_account_selection(self):
        response = self.client.get("/", headers=self.headers)
        banner = ElementTree.fromstring("<header>" + response.text.split("<header>", 1)[1].split("</header>", 1)[0] + "</header>")
        switch = urlsplit(banner.find(".//*[@id='switch-account']").attrib["href"])
        self.assertEqual((switch.scheme, switch.netloc, switch.path), ("", "", "/.auth/logout"))
        login = urlsplit(parse_qs(switch.query)["post_logout_redirect_uri"][0])
        self.assertEqual((login.scheme, login.netloc, login.path), ("", "", "/.auth/login/aad"))
        self.assertEqual(parse_qs(login.query), {"post_login_redirect_uri": ["/"], "prompt": ["select_account"]})

    def test_aca_principal_without_auth_type_requires_matching_verified_session(self):
        claims = json.loads(base64.b64decode(self.headers["x-ms-client-principal"]))["claims"]
        header = base64.b64encode(json.dumps({"claims": claims}).encode()).decode()
        headers = {**self.headers, "x-ms-client-principal": header}

        self.assertEqual(self.client.get("/api/dashboard", headers=headers).status_code, 200)
        self.assertEqual(self.client.get("/api/dashboard", headers={
            "x-ms-client-principal": header,
        }).status_code, 403)

    def test_upstream_errors_do_not_claim_that_a_write_was_not_applied(self):
        def disconnected(*args):
            raise RemoteError()

        self.client.app.state.operations.request = disconnected
        response = self.client.post("/api/operations", headers={
            **self.headers, "origin": "https://console.example.test", "x-dashboard-write": "1"
        }, json={"action": "deploy", "request_id": "83468040-d438-4007-bf2d-8068282a966d", "profile": "demo"})
        self.assertEqual(response.status_code, 503)
        self.assertIn("verify", response.json()["detail"].lower())
