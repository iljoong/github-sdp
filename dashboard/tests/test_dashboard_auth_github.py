import base64
import json
import threading
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
        self.assertEqual(actor.username, "operator@example.test")
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
        self.assertEqual(verifier(named).username, "operator@example.test")
        upn_only = {k: v for k, v in claims.items() if k != "preferred_username"}
        self.assertEqual(verifier(jwt.encode({**upn_only, "upn": "upn@example.test"}, private_key,
                                             algorithm="RS256")).username, "upn@example.test")
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

    def test_active_run_reads_refresh_after_short_ttl_and_completed_runs_do_not(self):
        now = [0]
        calls = []
        status = {1: "in_progress", 2: "completed"}

        def transport(method, url, headers, body):
            calls.append(url.split("/actions")[1])
            run_id = int(url.split("/runs/")[1].split("/")[0])
            if "/jobs" in url:
                return 200, {}, {"jobs": [{"name": "build / build", "status": status[run_id]}]}
            return 200, {}, {"id": run_id, "status": status[run_id]}

        observer = GitHub("owner/repo", lambda: "token", ReadCache(transport, clock=lambda: now[0]))

        def read_both():
            for run_id in (1, 2):
                observer.run(run_id)
                observer.jobs(run_id, 1)

        read_both()
        self.assertEqual(len(calls), 4)
        now[0] = 6
        read_both()
        self.assertEqual(sorted(calls[4:]), ["/runs/1", "/runs/1/attempts/1/jobs?per_page=100&page=1"])
        now[0] = 120
        read_both()
        self.assertEqual(len(calls), 8)
        now[0] = 400
        read_both()
        self.assertEqual(len(calls), 12)

    def test_runs_listed_as_completed_keep_their_job_and_gate_reads_for_the_completed_ttl(self):
        now, calls = [0], []

        def transport(method, url, headers, body):
            calls.append(url.split("/actions")[1])
            if "runs?" in url:
                return 200, {}, {"workflow_runs": [{"id": 3, "status": "completed"}, {"id": 4, "status": "in_progress"}]}
            return 200, {}, {"jobs": []} if "/jobs" in url else []

        observer = GitHub("owner/repo", lambda: "token", ReadCache(transport, clock=lambda: now[0]))
        observer.runs(recent=True)
        for run_id in (3, 4):
            observer.jobs(run_id, 1)
            observer.pending(run_id)
        del calls[:]
        now[0] = 120
        for run_id in (3, 4):
            observer.jobs(run_id, 1)
            observer.pending(run_id)
        self.assertEqual(calls, ["/runs/4/attempts/1/jobs?per_page=100&page=1", "/runs/4/pending_deployments"])

    def test_concurrent_identical_reads_share_one_upstream_call(self):
        calls, entered, release = [], threading.Event(), threading.Event()

        def transport(method, url, headers, body):
            calls.append(url)
            entered.set()
            release.wait(5)
            # Not cacheable, so late callers would make their own call unless they joined the flight.
            return 502, {}, None

        observer = GitHub("owner/repo", lambda: "token", ReadCache(transport))
        outcomes = []

        def read():
            try:
                observer.run(1)
            except RemoteError as error:
                outcomes.append(error.status)

        readers = [threading.Thread(target=read) for _ in range(4)]
        readers[0].start()
        self.assertTrue(entered.wait(5))
        for thread in readers[1:]:
            thread.start()
        time.sleep(0.2)
        release.set()
        for thread in readers:
            thread.join(5)
        self.assertEqual(len(calls), 1)
        self.assertEqual(outcomes, [502] * 4)

    def test_rate_limit_backoff_is_shared_and_reports_remaining_seconds(self):
        now, calls = [100], []

        def transport(method, url, headers, body):
            calls.append(url)
            if len(calls) == 1:
                return 429, {"retry-after": "30"}, None
            return 200, {}, {"status": "completed"}

        cache = ReadCache(transport, clock=lambda: now[0])
        observer = GitHub("owner/repo", lambda: "token", cache)
        self.assertEqual(cache.backoff_remaining(), 0)
        with self.assertRaises(RemoteError):
            observer.run(1)
        now[0] = 110
        self.assertEqual(cache.backoff_remaining(), 20)
        with self.assertRaises(RemoteError) as raised:
            observer.run(2)
        self.assertEqual((raised.exception.status, len(calls)), (429, 1))
        now[0] = 131
        self.assertEqual(cache.backoff_remaining(), 0)
        self.assertEqual(observer.run(2)["status"], "completed")

    def test_transport_reuses_one_strict_client(self):
        import httpx
        from unittest import mock

        import dashboard.github as github_module

        created = []
        real = httpx.Client

        def client(**settings):
            created.append(settings)
            return real(transport=httpx.MockTransport(lambda request: httpx.Response(
                302, headers={"location": "https://unrelated.test"}, json={})), **settings)

        github_module.close_transport()
        with mock.patch.object(httpx, "Client", client):
            try:
                for _ in range(2):
                    self.assertEqual(github_module.transport("GET", "https://api.github.com/x", {}, None)[0], 302)
            finally:
                github_module.close_transport()
        self.assertEqual(len(created), 1)
        self.assertEqual((created[0]["follow_redirects"], created[0]["trust_env"]), (False, False))

    def test_slow_read_does_not_block_a_different_url(self):
        entered, release = threading.Event(), threading.Event()

        def transport(method, url, headers, body):
            if url.endswith("/runs/1"):
                entered.set()
                release.wait(5)
            return 200, {}, {"status": "completed"}

        observer = GitHub("owner/repo", lambda: "token", ReadCache(transport))
        slow = threading.Thread(target=observer.run, args=(1,))
        slow.start()
        try:
            self.assertTrue(entered.wait(5))
            fast = threading.Thread(target=observer.run, args=(2,))
            fast.start()
            fast.join(2)
            self.assertFalse(fast.is_alive())
        finally:
            release.set()
            slow.join(5)

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

    def test_release_dispatch_accepts_only_well_formed_resume_inputs(self):
        sent = []
        gh = GitHub("owner/repo", lambda: "token", lambda *args: sent.append(args[3]) or (200, {}, {"workflow_run_id": 9}))
        resume = {"profile": "production", "resume_from_run_id": "123", "start_region": "sea"}
        self.assertEqual(gh.dispatch("release.yml", resume), 9)
        self.assertEqual(sent[0]["inputs"], resume)
        for bad in ({**resume, "start_region": "all"}, {**resume, "resume_from_run_id": "0"},
                    {**resume, "resume_from_run_id": "12a"}, {"profile": "demo", "resume_from_run_id": "123"}):
            with self.assertRaises(ValueError):
                gh.dispatch("release.yml", bad)
        self.assertEqual(len(sent), 1)

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
