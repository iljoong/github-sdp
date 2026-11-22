import hashlib
import hmac
import json
import unittest
from types import SimpleNamespace
from uuid import uuid4

from azure.core.exceptions import HttpResponseError, ResourceExistsError, ResourceNotFoundError

from dashboard.auth import Actor, OPERATOR
from dashboard.config import SharedSettings
from dashboard.operations import Command, Operations
from dashboard.store import OperationsTable
from dashboard.webhook import RuleEvent, Rejected, parse_rule, receive

SETTINGS = SharedSettings("owner/repo", 23, 8, "https://table.example.test")
SECRET = b"known test fixture"
DELIVERY = "65898b44-84e6-40a4-bfd8-f598e7090f0d"


def event(**changes):
    payload = {"action": "requested", "environment": "prod-sea", "event": "workflow_dispatch",
               "ref": "refs/heads/main", "sha": "a" * 40,
               "deployment_callback_url": "https://api.github.com/repos/owner/repo/actions/runs/401/deployment_protection_rule",
               "repository": {"id": 23, "full_name": "owner/repo"}, "installation": {"id": 8}}
    payload.update(changes)
    body = json.dumps(payload).encode()
    return {"x-github-event": "deployment_protection_rule", "x-github-delivery": DELIVERY,
            "x-hub-signature-256": "sha256=" + hmac.new(SECRET, body, hashlib.sha256).hexdigest()}, body


class WebhookTests(unittest.TestCase):
    def test_signed_gate_extracts_run_from_exact_callback(self):
        headers, body = event()
        self.assertEqual(parse_rule(headers, body, SECRET, SETTINGS),
                         RuleEvent(DELIVERY, 401, "prod-sea", "a" * 40))

    def test_wrong_targets_do_not_enable_gate_even_when_signed(self):
        for changes in ({"environment": "staging"}, {"installation": {"id": 3}},
                        {"repository": {"id": 24, "full_name": "owner/repo"}},
                        {"deployment_callback_url": "https://elsewhere.test/repos/owner/repo/actions/runs/401/deployment_protection_rule"}):
            with self.subTest(changes=changes), self.assertRaises(Rejected):
                parse_rule(*event(**changes), SECRET, SETTINGS)

    def test_tampering_and_oversized_input_are_rejected(self):
        headers, body = event()
        with self.assertRaises(Rejected):
            parse_rule(headers, body + b" ", SECRET, SETTINGS)
        with self.assertRaises(Rejected):
            parse_rule(headers, b" " * (1_048_576 + 1), SECRET, SETTINGS)

    def test_repeat_delivery_is_acknowledged_without_second_gate(self):
        rows = {}

        class Inbox:
            def put(self, rule):
                if rule.delivery_id in rows:
                    return False
                rows[rule.delivery_id] = rule
                return True

        self.assertTrue(receive(*event(), SECRET, SETTINGS, Inbox()))
        self.assertFalse(receive(*event(), SECRET, SETTINGS, Inbox()))
        self.assertEqual(len(rows), 1)


class Entity(dict):
    def __init__(self, values, etag):
        super().__init__(values)
        self.metadata = {"etag": etag}


class FakeTable:
    """Behaves like the Azure Tables SDK boundary: string values, RowKey-ordered range queries, atomic batches."""

    def __init__(self, rows=()):
        self.rows, self.version, self.ranges = {}, 0, []
        for row in rows:
            self._put(row)

    def _put(self, row):
        self.version += 1
        self.rows[(row["PartitionKey"], row["RowKey"])] = Entity(row, f"e{self.version}")

    def create_entity(self, row):
        if (row["PartitionKey"], row["RowKey"]) in self.rows:
            raise ResourceExistsError("exists")
        self._put(row)

    def get_entity(self, partition, key):
        if (partition, key) not in self.rows:
            raise ResourceNotFoundError("missing")
        row = self.rows[(partition, key)]
        return Entity(row, row.metadata["etag"])

    def query_entities(self, query, parameters, results_per_page=None):
        low, high = parameters["low"], parameters["high"]
        self.ranges.append(low)
        for (partition, key), row in sorted(self.rows.items()):
            if partition == parameters["p"] and low <= key < high:
                yield Entity(row, row.metadata["etag"])

    def submit_transaction(self, changes):
        staged = dict(self.rows)
        for change in changes:
            kind, row = change[0], change[1]
            key = (row["PartitionKey"], row["RowKey"])
            current = staged.get(key)
            etag = change[2].get("etag") if len(change) > 2 else None
            if (kind == "create" and current is not None) or (kind in ("update", "delete") and (
                    current is None or (etag and current.metadata["etag"] != etag))):
                error = HttpResponseError("conflict")
                error.status_code = 409
                raise error
            self.version += 1
            if kind == "delete":
                del staged[key]
            else:
                staged[key] = Entity(row, f"e{self.version}")
        self.rows = staged


class AuditStoreTests(unittest.TestCase):
    def setUp(self):
        self.now = 1_800_000_000.0
        self.table = FakeTable()
        self.store = OperationsTable(self.table, "23", clock=lambda: self.now)

    def record(self, run_id=None, **values):
        key = str(uuid4())
        self.store.reserve(key, {"kind": "deploy", **values})
        self.store.finish(key, {"status": "accepted", **({"run_id": run_id} if run_id else {})})
        self.now += 1
        return key

    def test_recent_is_newest_first_without_scanning_operation_rows(self):
        self.store.recent()
        keys = [self.record() for _ in range(3)]
        self.table.ranges.clear()
        rows = self.store.recent(limit=2)
        self.assertEqual([row["operation_id"] for row in rows], [keys[2], keys[1]])
        self.assertEqual(rows[0]["status"], "accepted")
        self.assertNotIn("op:", self.table.ranges)

    def test_run_index_and_display_name_round_trip_for_large_run_ids(self):
        run_id = 19123456789
        workflow = SimpleNamespace(dispatch=lambda workflow, inputs: run_id)
        ops = Operations(self.store, workflow, SimpleNamespace(check=lambda command: None))
        actor = Actor("tenant", "user-1", frozenset({OPERATOR}), "Kim Release")
        outcome = ops.request(actor, Command("deploy", str(uuid4()), profile="demo"))
        self.assertEqual(self.table.rows[("23", f"op:{outcome['operation_id']}")]["run_id"], "19123456789")
        row = self.store.by_run(run_id)
        self.assertEqual((row["operation_id"], row["run_id"], row["actor_name"]),
                         (outcome["operation_id"], run_id, "Kim Release"))
        self.assertEqual(self.store.recent()[0]["actor_name"], "Kim Release")
        self.assertIsNone(self.store.by_run(run_id + 1))

    def test_rows_written_before_indexes_remain_readable(self):
        legacy = [{"PartitionKey": "23", "RowKey": f"op:{key}", "operation_id": key, "kind": "deploy",
                   "status": "accepted", "created_at": created, "run_id": run_id, "actor": "user-1"}
                  for key, created, run_id in (("old-a", "2026-11-01T00:00:00+00:00", "401"),
                                               ("old-b", "2026-11-02T00:00:00.500000+00:00", "19123456789"))]
        self.table = FakeTable([*legacy, {"PartitionKey": "23", "RowKey": "claim:cancel:401", "owner": "old-a"}])
        self.store = OperationsTable(self.table, "23", clock=lambda: self.now)
        self.assertEqual([row["operation_id"] for row in self.store.recent()], ["old-b", "old-a"])
        self.assertEqual(self.store.by_run(401)["operation_id"], "old-a")
        self.assertEqual(self.store.by_run(19123456789)["run_id"], 19123456789)
        self.assertTrue(self.store.claimed("cancel:401"))
        newest = self.record(run_id=402)
        self.table.ranges.clear()
        self.assertEqual([row["operation_id"] for row in self.store.recent()], [newest, "old-b", "old-a"])
        self.assertNotIn("op:", self.table.ranges)
        fresh = OperationsTable(self.table, "23", clock=lambda: self.now)
        self.assertEqual(fresh.by_run(402)["operation_id"], newest)
        self.assertNotIn("op:", self.table.ranges)