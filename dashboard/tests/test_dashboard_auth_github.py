import base64
import json
import time
import unittest
from types import SimpleNamespace

import jwt
from cryptography.hazmat.primitives.asymmetric import rsa

from dashboard.auth import Denied, EntraSession, actor_from_principal, authorize, verify_write_origin
from dashboard.github import GitHub, ReadCache, RemoteError, Tokens


TENANT = "24373433-0a85-45ce-96b6-baf8530bc884"
USER = "6a07c4ce-08fd-46aa-9e45-ad65560943c6"


def principal(*roles, tenant=TENANT, user=USER, kind="user"):
    claims = {"tid": tenant, "oid": user, "idtyp": kind, "preferred_username": "operator@example.test"}
    return base64.b64encode(json.dumps({
        "auth_typ": "aad", "role_typ": "roles",
        "claims": [{"typ": k, "val": v} for k, v in claims.items()]
        + [{"typ": "roles", "val": r} for r in roles],
    }).encode()).decode()


class AuthTests(unittest.TestCase):
    def test_require_the_tenant_a_user_and_an_assigned_role(self):
        actor = actor_from_principal(principal("ReleaseApprover"), TENANT)
        self.assertEqual(actor.object_id, USER)
        authorize(actor, "ReleaseApprover")
        for header in (None, principal(), principal("ReleaseOperator", kind="app"),
                       principal("ReleaseOperator", tenant=USER)):
            with self.subTest(header=header), self.assertRaises(Denied):
                actor_from_principal(header, TENANT)
        with self.assertRaises(Denied):
            authorize(actor, "ReleaseOperator")

    def test_write_needs_exact_origin_and_console_header(self):
        origin = "https://console.example.test"
        verify_write_origin({"origin": origin, "x-dashboard-write": "1"}, origin)
        for headers in ({}, {"origin": origin}, {"origin": origin + ".other", "x-dashboard-write": "1"}):
            with self.assertRaises(Denied):
                verify_write_origin(headers, origin)

    def test_signed_session_cannot_substitute_another_application_or_tenant(self):
        private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        verifier = EntraSession(TENANT, "console-client")
        verifier.keys = SimpleNamespace(get_signing_key_from_jwt=lambda _: SimpleNamespace(key=private_key.public_key()))
        now = int(time.time())
        claims = {"iss": f"https://login.microsoftonline.com/{TENANT}/v2.0", "aud": "console-client",
                  "iat": now, "nbf": now, "exp": now + 60, "tid": TENANT, "oid": USER,
                  "roles": ["ReleaseOperator"], "preferred_username": "operator@example.test"}
        self.assertEqual(verifier(jwt.encode(claims, private_key, algorithm="RS256")).object_id, USER)
        self.assertEqual(verifier(jwt.encode(claims, private_key, algorithm="RS256")).display_name,
                         "operator@example.test")
        named = jwt.encode({**claims, "name": "Test Operator"}, private_key, algorithm="RS256")
        self.assertEqual(verifier(named).display_name, "Test Operator")
        for override in ({"aud": "other-client"}, {"tid": USER}, {"exp": now - 60}):
            with self.assertRaises(Denied):
                verifier(jwt.encode({**claims, **override}, private_key, algorithm="RS256"))


class GitHubTests(unittest.TestCase):
    def test_observer_conditional_cache_does_not_cache_fresh_checks(self):
        now = [0]
        seen = []

        def transport(method, url, headers, body):
            seen.append(headers.get("If-None-Match"))
            if headers.get("If-None-Match") == '"sample"':
                return 304, {}, None
            return 200, {"etag": '"sample"'}, {"status": "waiting"}

        dashboard = GitHub("owner/repo", lambda: "token", ReadCache(transport, clock=lambda: now[0]))
        live = GitHub("owner/repo", lambda: "token", transport)
        dashboard.run(123)
        dashboard.run(123)
        live.run(123)
        now[0] = 45
        self.assertEqual(dashboard.run(123)["status"], "waiting")
        self.assertEqual(seen, [None, None, '"sample"'])

    def test_dispatch_correlates_only_with_returned_id_and_fixes_ref(self):
        calls = []

        def transport(method, url, headers, body):
            calls.append((method, url, body))
            return 200, {}, {"workflow_run_id": 123}

        gh = GitHub("owner/repo", lambda: "token", transport)
        self.assertEqual(gh.dispatch("release.yml", {"profile": "demo"}), 123)
        self.assertEqual(calls, [("POST", "https://api.github.com/repos/owner/repo/actions/workflows/release.yml/dispatches",
                                  {"ref": "main", "inputs": {"profile": "demo"}, "return_run_details": True})])
        with self.assertRaises(ValueError):
            gh.dispatch("../evil.yml", {})
        with self.assertRaises(ValueError):
            gh.dispatch("restore.yml", {"region": "all", "source_run_id": "123"})

    def test_api_redirect_is_not_followed_and_dispatch_without_id_uncertain(self):
        for response in [(302, {"location": "https://unrelated.test"}, {}), (204, {}, None)]:
            calls = []

            def transport(*args):
                calls.append(args)
                return response

            with self.assertRaises(RemoteError):
                GitHub("owner/repo", lambda: "token", transport).dispatch("release.yml", {"profile": "demo"})
            self.assertEqual(len(calls), 1)

    def test_installation_token_expires_and_is_repo_scoped(self):
        now = [1000]
        payloads = []
        signed = []

        def transport(method, url, headers, body):
            payloads.append(body)
            self.assertIn("Bearer ", headers["Authorization"])
            return 201, {}, {"token": f"token{len(payloads)}", "expires_at": "1970-01-01T01:00:00Z"}

        def sign(digest):
            signed.append(digest)
            return b"signature"

        token = Tokens("Iv1.app", 7, 42, sign, transport, lambda: now[0])
        self.assertEqual(token(), "token1")
        self.assertEqual(token(), "token1")
        now[0] = 3590
        self.assertEqual(token(), "token2")
        self.assertTrue(all(len(digest) == 32 for digest in signed))
        self.assertEqual(payloads[0], {"repository_ids": [42], "permissions": {"actions": "write", "deployments": "write"}})
