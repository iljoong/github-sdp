import hashlib
import hmac
import json
import unittest
from types import SimpleNamespace
from uuid import uuid4

from azure.core.exceptions import ResourceExistsError
from fastapi.testclient import TestClient

from dashboard.auth import Actor, OPERATOR
from dashboard.changes import ChangeFeed
from dashboard.server import create_app
from dashboard.store import Inbox
from dashboard.webhook import Rejected, receive
from test_dashboard_auth_github import TENANT, USER, principal
from test_dashboard_store_webhook import DELIVERY, SECRET, SETTINGS, event


class SDKTable:
    """Azure Tables SDK boundary keeping rows in memory (insert-only, range queries)."""

    def __init__(self):
        self.rows = {}

    def create_entity(self, entity):
        key = (entity["PartitionKey"], entity["RowKey"])
        if key in self.rows:
            raise ResourceExistsError("exists")
        self.rows[key] = dict(entity)

    def query_entities(self, query, parameters, **kwargs):
        low, high = parameters["low"], parameters["high"]
        return iter(sorted((dict(row) for (p, key), row in self.rows.items()
                            if p == parameters["p"] and low < key < high), key=lambda row: row["RowKey"]))


def signed(kind, payload, delivery=DELIVERY):
    body = json.dumps(payload).encode()
    return {"x-github-event": kind, "x-github-delivery": delivery,
            "x-hub-signature-256": "sha256=" + hmac.new(SECRET, body, hashlib.sha256).hexdigest()}, body


def run_event(run=None, **changes):
    workflow_run = {"id": 401, "run_attempt": 2, "path": ".github/workflows/release.yml",
                    "head_branch": "main", "event": "workflow_dispatch",
                    "repository": {"id": 23, "full_name": "owner/repo"}}
    workflow_run.update(run or {})
    payload = {"action": "in_progress", "workflow_run": workflow_run,
               "repository": {"id": 23, "full_name": "owner/repo"}, "installation": {"id": 8}}
    payload.update(changes)
    return "workflow_run", payload


def job_event(job=None, **changes):
    workflow_job = {"id": 9001, "run_id": 401, "run_attempt": 1, "head_branch": "main",
                    "workflow_name": "Safe ACA release - Korea then Southeast Asia then West US"}
    workflow_job.update(job or {})
    payload = {"action": "waiting", "workflow_job": workflow_job,
               "repository": {"id": 23, "full_name": "owner/repo"}, "installation": {"id": 8}}
    payload.update(changes)
    return "workflow_job", payload


class ChangeEventTests(unittest.TestCase):
    def setUp(self):
        self.table = SDKTable()
        self.inbox = Inbox(self.table, "23")

    def receive(self, kind, payload, delivery=DELIVERY):
        return receive(*signed(kind, payload, delivery), SECRET, SETTINGS, self.inbox)

    def test_signed_run_and_job_events_are_stored_once_per_delivery(self):
        job_delivery = str(uuid4())
        self.assertTrue(self.receive(*run_event()))
        self.assertTrue(self.receive(*job_event(), delivery=job_delivery))
        self.assertFalse(self.receive(*run_event()))
        self.assertFalse(self.receive(*job_event(), delivery=job_delivery))
        changes = self.inbox.changes()
        self.assertEqual([(c["run_id"], c["run_attempt"], c["kind"], c["delivery_id"]) for c in changes],
                         [(401, 2, "workflow_run", DELIVERY), (401, 1, "workflow_job", job_delivery)])
        self.assertEqual(self.inbox.rules(401), [])

    def test_changes_after_a_cursor_are_returned_in_arrival_order(self):
        first, second = str(uuid4()), str(uuid4())
        self.receive(*run_event(), delivery=first)
        cursor = self.inbox.changes()[-1]["RowKey"]
        self.receive(*job_event({"run_id": 402}), delivery=second)
        self.assertEqual([c["run_id"] for c in self.inbox.changes(after=cursor)], [402])

    def test_unsigned_foreign_and_malformed_events_are_rejected_without_rows(self):
        rejected = [
            run_event(repository={"id": 24, "full_name": "owner/repo"}),
            run_event(installation={"id": 3}),
            run_event({"repository": {"id": 24, "full_name": "other/repo"}}),
            run_event({"id": "401; drop"}),
            run_event({"run_attempt": 0}),
            job_event(installation={"id": 3}),
            job_event(repository={"id": 23, "full_name": "other/repo"}),
            job_event({"run_id": 0}),
        ]
        for kind, payload in rejected:
            with self.subTest(kind=kind, payload=payload), self.assertRaises(Rejected):
                self.receive(kind, payload)
        headers, body = signed(*run_event())
        with self.assertRaises(Rejected):
            receive({**headers, "x-hub-signature-256": "sha256=" + "0" * 64}, body, SECRET, SETTINGS, self.inbox)
        self.assertEqual(self.table.rows, {})

    def test_non_main_and_non_allowlisted_workflows_are_acknowledged_but_ignored(self):
        ignored = [
            run_event({"head_branch": "feature"}),
            run_event({"path": ".github/workflows/oidc-inspect.yml"}),
            run_event({"event": "push"}),
            job_event({"head_branch": "feature"}),
            job_event({"workflow_name": "Inspect actual protected environment OIDC claims - no Azure access"}),
        ]
        for kind, payload in ignored:
            with self.subTest(kind=kind, payload=payload):
                self.assertFalse(self.receive(kind, payload, str(uuid4())))
        self.assertEqual(self.table.rows, {})

    def test_protection_rule_events_still_create_gate_rows_only(self):
        self.assertTrue(receive(*event(), SECRET, SETTINGS, self.inbox))
        self.assertEqual(len(self.inbox.rules(401)), 1)
        self.assertEqual(self.inbox.changes(), [])


class ChangeStreamTests(unittest.TestCase):
    def setUp(self):
        test = self

        class DeliveredWhileConnected(Inbox):
            """A run event reaches the receiver after the browser has connected (on the second inbox read)."""
            reads = 0

            def changes(self, *args, **kwargs):
                self.reads += 1
                if self.reads == 2:
                    test.assertTrue(receive(*signed(*job_event({"run_id": 19123456789})), SECRET, SETTINGS, self))
                return super().changes(*args, **kwargs)

        self.invalidated = []
        self.inbox = DeliveredWhileConnected(SDKTable(), "23")
        app = create_app()
        app.state.settings = SimpleNamespace(tenant_id=TENANT, origin="https://console.example.test")
        app.state.verify_session = lambda token: Actor(TENANT, USER, frozenset({OPERATOR})) if token == "valid-fixture" else None
        app.state.reader = SimpleNamespace(snapshot=lambda actor, run_id, region: {"run": run_id})
        app.state.changes = ChangeFeed(self.inbox, self.invalidated.append, interval=0)
        app.state.stream_timing = (0.01, 0.3)
        self.client = TestClient(app, base_url="https://console.example.test")
        self.headers = {"x-ms-client-principal": principal(OPERATOR), "x-ms-token-aad-id-token": "valid-fixture"}

    def test_stream_requires_the_same_principal_and_session_as_the_api(self):
        self.assertEqual(self.client.get("/api/changes").status_code, 403)
        self.assertEqual(self.client.get("/api/changes", headers={
            "x-ms-client-principal": self.headers["x-ms-client-principal"]}).status_code, 403)
        unassigned = {**self.headers, "x-ms-client-principal": principal()}
        self.assertEqual(self.client.get("/api/changes", headers=unassigned).status_code, 403)
        self.assertEqual(self.inbox.reads, 0)

    def test_stream_announces_only_the_changed_run_when_the_cursor_advances(self):
        with self.client.stream("GET", "/api/changes", headers=self.headers) as response:
            self.assertEqual(response.status_code, 200)
            self.assertTrue(response.headers["content-type"].startswith("text/event-stream"))
            self.assertEqual(response.headers["cache-control"], "no-store")
            body = response.read().decode()
        messages = [block for block in body.split("\n\n") if block.startswith("event:")]
        self.assertEqual(messages, ['event: change\ndata: {"run_id": 19123456789}'])
        self.assertEqual(self.invalidated, [19123456789])

    def test_snapshot_requests_also_apply_new_hints_when_no_stream_is_open(self):
        receive(*signed(*run_event()), SECRET, SETTINGS, self.inbox)
        self.assertEqual(self.client.get("/api/dashboard?run_id=401", headers=self.headers).status_code, 200)
        self.assertEqual(self.invalidated, [401])
