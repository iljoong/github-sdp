import base64
import json
import re
import unittest
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit
from xml.etree import ElementTree

from fastapi.testclient import TestClient

from dashboard.auth import APPROVER, Actor, OPERATOR, SessionExpired
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
                                                "runtime:app-sea--r5", "runtime:app-sea--r7", "hints"})
        self.assertEqual(set(data["sources"]["github"]), {"observed_at", "age_seconds", "state"})
        self.assertEqual(data["sources"]["runtime:app-sea--r5"]["reason"], "not_produced")
        self.assertEqual(data["inbox"], [])
        self.assertEqual(set(data["regions"]["kr"]["progress"]),
                         {"step", "started_at", "elapsed_seconds", "failed_step", "log_url"})
        self.assertEqual(data["regions"]["kr"]["traffic_stage"], 0)
        self.assertTrue({"run", "runs", "regions", "build", "pending", "actions", "audit", "observed_at",
                         "github_available", "audit_available"} <= set(data))

    def test_console_assets_stay_within_the_content_security_policy(self):
        page = self.client.get("/", headers=self.headers)
        csp = page.headers["content-security-policy"]
        self.assertIn("script-src 'self'", csp)
        self.assertIn("style-src 'self'", csp)
        self.assertNotIn("unsafe-inline", csp)
        self.assertNotRegex(page.text, r"<script(?![^>]*\bsrc=)[^>]*>")
        self.assertNotRegex(page.text, r"<style|\sstyle=|\son[a-z]+=")
        script = self.client.get("/assets/dashboard.js", headers=self.headers)
        self.assertEqual(script.status_code, 200)
        self.assertIn("default-src 'self'", script.headers["content-security-policy"])
        # Data is rendered as DOM text only; no HTML sinks or string evaluation.
        self.assertNotRegex(script.text, r"innerHTML|outerHTML|insertAdjacentHTML|document\.write|\beval\(|new Function|\.style\b")

    def test_deep_link_to_a_release_and_region_opens_the_console_with_live_controls(self):
        page = self.client.get("/?run=34&region=sea", headers=self.headers)
        self.assertEqual(page.status_code, 200)
        ids = set(re.findall(r'\sid="([^"]+)"', page.text))
        self.assertLessEqual({"inbox", "live", "refresh", "notify", "freshness", "connection", "regional", "releases",
                              "deploy", "tab-inbox", "inbox-count"}, ids)
        self.assertEqual(self.client.get("/?run=34&region=sea").status_code, 403)

    def test_writes_need_origin_and_reject_extra_targets(self):
        command = {"action":"deploy","request_id":"83468040-d438-4007-bf2d-8068282a966d","profile":"demo"}
        self.assertEqual(self.client.post("/api/operations", json=command, headers=self.headers).status_code, 403)
        headers = {**self.headers, "origin":"https://console.example.test","x-dashboard-write":"1"}
        self.assertEqual(self.client.post("/api/operations", json=command, headers=headers).status_code, 200)
        self.assertEqual(self.client.post("/api/operations", json={**command,"repo":"other/repo"}, headers=headers).status_code, 422)

    def test_resume_is_posted_with_only_a_request_id_and_source_release(self):
        headers = {**self.headers, "origin":"https://console.example.test","x-dashboard-write":"1"}
        commands = []
        self.client.app.state.operations.request = lambda actor, command: commands.append(command) or {"status": "accepted"}
        resume = {"action":"resume","request_id":"83468040-d438-4007-bf2d-8068282a966d","source_run_id":7}
        self.assertEqual(self.client.post("/api/operations", json=resume, headers=headers).status_code, 200)
        self.assertEqual((commands[0].action, commands[0].source_run_id, commands[0].region), ("resume", 7, None))
        self.assertEqual(self.client.post("/api/operations", json={**resume,"region":"usw"}, headers=headers).status_code, 422)

    def banner(self, response):
        header = re.search(r"<header\b.*?</header>", response.text, re.S).group(0)
        return ElementTree.fromstring(header)

    def test_banner_uses_verified_account_name_and_all_dashboard_roles(self):
        headers = {**self.headers, "x-ms-client-principal": principal(OPERATOR, APPROVER)}
        self.client.app.state.verify_session = lambda _: SimpleNamespace()
        self.assertEqual(self.client.get("/", headers=headers).status_code, 403)
        self.client.app.state.verify_session = lambda _: Actor(
            TENANT, USER, frozenset({OPERATOR, APPROVER}), display_name="Test <Admin>", username="t<o>m&\"x@example.test")
        response = self.client.get("/", headers=headers)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers["cache-control"], "no-store")
        banner = self.banner(response)
        self.assertEqual(banner.find(".//*[@id='account-name']").text, "Test <Admin>")
        self.assertEqual(banner.find(".//*[@id='account-alias']").text, 't<o>m&"x')
        self.assertIn("Test <Admin>", banner.find(".//summary").attrib["aria-label"])
        self.assertEqual([item.text for item in banner.findall(".//span[@class='account-role']")],
                         ["Approver", "Operator"])
        self.assertNotIn("<Admin>", response.text)

    def test_alias_falls_back_to_the_first_word_of_the_display_name(self):
        self.client.app.state.verify_session = lambda _: Actor(
            TENANT, USER, frozenset({OPERATOR}), display_name="Kim Operator")
        self.assertEqual(self.banner(self.client.get("/", headers=self.headers)).find(".//*[@id='account-alias']").text,
                         "Kim")

    def test_switch_account_returns_from_easy_auth_logout_to_entra_account_selection(self):
        response = self.client.get("/", headers=self.headers)
        banner = self.banner(response)
        self.assertEqual(banner.find(".//*[@id='sign-out']").attrib["href"], "/.auth/logout")
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

    def expire_sessions(self):
        operator = Actor(TENANT, USER, frozenset({OPERATOR}))

        def verify(token):
            if token == "expired-fixture":
                raise SessionExpired(operator)
            return operator if token == "valid-fixture" else None

        self.client.app.state.verify_session = verify
        return {**self.headers, "x-ms-token-aad-id-token": "expired-fixture"}

    def test_expired_session_is_reported_as_renewable_and_writes_are_not_applied(self):
        expired = self.expire_sessions()
        commands = []
        self.client.app.state.operations.request = lambda actor, command: commands.append(command) or {}
        self.client.app.state.operations.review = lambda *args: commands.append(args) or {}
        write = {**expired, "origin": "https://console.example.test", "x-dashboard-write": "1"}
        command = {"action": "deploy", "request_id": "83468040-d438-4007-bf2d-8068282a966d", "profile": "demo"}
        for response in (self.client.get("/api/dashboard", headers=expired),
                         self.client.get("/assets/dashboard.js", headers=expired),
                         self.client.get("/api/changes", headers=expired),
                         self.client.post("/api/operations", json=command, headers=write),
                         self.client.post("/api/operations/op-1/review", json={"environment": "prod-kr", "approved": True},
                                          headers=write)):
            with self.subTest(response.request.url.path):
                self.assertEqual(response.status_code, 401)
                self.assertEqual(response.json(), {"detail": "Entra session expired"})
        self.assertEqual(commands, [])

    def test_reloading_the_console_with_an_expired_session_signs_in_again_and_returns(self):
        expired = self.expire_sessions()
        response = self.client.get("/?run=34&region=sea", headers=expired, follow_redirects=False)
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.headers["cache-control"], "no-store")
        login = urlsplit(response.headers["location"])
        self.assertEqual((login.scheme, login.netloc, login.path), ("", "", "/.auth/login/aad"))
        self.assertEqual(parse_qs(login.query), {"post_login_redirect_uri": ["/?run=34&region=sea"]})
        plain = urlsplit(self.client.get("/", headers=expired, follow_redirects=False).headers["location"])
        self.assertEqual(parse_qs(plain.query), {"post_login_redirect_uri": ["/"]})

    def test_expired_session_does_not_override_other_session_failures(self):
        expired = self.expire_sessions()
        no_principal = {"x-ms-token-aad-id-token": "expired-fixture"}
        other_user = {**expired, "x-ms-client-principal": principal(OPERATOR, user=TENANT)}
        unassigned = {**expired, "x-ms-client-principal": principal()}
        for name, headers in (("no principal", no_principal), ("other user", other_user), ("unassigned", unassigned)):
            with self.subTest(name):
                self.assertEqual(self.client.get("/api/dashboard", headers=headers).status_code, 403)
