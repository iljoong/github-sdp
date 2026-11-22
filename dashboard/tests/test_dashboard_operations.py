import unittest
from types import SimpleNamespace
from uuid import uuid4

from dashboard.auth import Actor, APPROVER, OPERATOR, Denied
from dashboard.github import GitHub, ReadCache, RemoteError
from dashboard.operations import Command, InvalidAction, Operations
from dashboard.store import Conflict


class Table:
    def __init__(self):
        self.rows, self.locks = {}, {}

    def get(self, key):
        return self.rows.get(key)

    def reserve(self, key, values, slot="write"):
        if key in self.rows or slot in self.locks:
            raise Conflict("Operation already requested")
        self.rows[key] = {**values, "operation_id": key, "status": "reserved"}
        self.locks[slot] = key

    def finish(self, key, updates, slot="write", release=True):
        self.rows[key].update(updates)
        if release:
            del self.locks[slot]

    def by_run(self, run_id):
        return next((row for row in self.rows.values() if row.get("run_id") == run_id), None)

    def claimed(self, claim):
        return any(row.get("claim") == claim and row["status"] == "accepted" for row in self.rows.values())


class OperationsTests(unittest.TestCase):
    def setUp(self):
        self.store = Table()
        self.workflow = SimpleNamespace(runs=lambda *a, **k: [], dispatch=self.dispatch,
                                        review_rule=self.review, cancel=lambda run_id: None)
        self.reader = SimpleNamespace(check=lambda command: None, gate=lambda row, name: name == "prod-kr")
        self.ops = Operations(self.store, self.workflow, self.reader)
        self.operator = Actor("tenant", "user-1", frozenset({OPERATOR, APPROVER}))
        self.approver = Actor("tenant", "user-2", frozenset({APPROVER}))
        self.key = str(uuid4())

    def dispatch(self, workflow, inputs):
        self.assertIn("write", self.store.locks)
        return 234

    def review(self, run_id, env, approved, comment):
        self.assertEqual((run_id, env, approved), (234, "prod-kr", True))

    def test_self_review_is_rejected_and_separate_actor_can_review_once(self):
        self.ops.request(self.operator, Command("deploy", self.key, profile="demo"))
        with self.assertRaises(Denied):
            self.ops.review(self.operator, self.key, "prod-kr", True)
        with self.assertRaises(InvalidAction):
            self.ops.review(self.approver, self.key, "prod-sea", True)
        outcome = self.ops.review(self.approver, self.key, "prod-kr", True)
        self.assertEqual(self.store.get(outcome["operation_id"])["status"], "accepted")
        with self.assertRaises(Conflict):
            self.ops.review(self.approver, self.key, "prod-kr", True)

    def test_review_can_retry_after_confirmed_no_write(self):
        self.ops.request(self.operator, Command("deploy", self.key, profile="demo"))

        def precondition_failure(*unused):
            raise RemoteError(403)

        self.workflow.review_rule = precondition_failure
        previous = self.ops.review(self.approver, self.key, "prod-kr", True)
        self.assertEqual(previous["status"], "rejected")
        self.workflow.review_rule = self.review
        outcome = self.ops.review(self.approver, self.key, "prod-kr", True)
        self.assertNotEqual(previous["operation_id"], outcome["operation_id"])
        self.assertEqual(outcome["status"], "accepted")

    def test_uncertain_dispatch_remains_reserved_against_conflicting_writes(self):
        def drop(*unused):
            raise RemoteError()

        self.workflow.dispatch = drop
        outcome = self.ops.request(self.operator, Command("deploy", self.key, profile="production"))
        self.assertEqual(outcome["status"], "confirmation_required")
        with self.assertRaises(Conflict):
            self.ops.request(self.operator, Command("deploy", str(uuid4()), profile="production"))

    def test_writes_invalidate_cached_reads_of_the_affected_run(self):
        calls = []

        def transport(method, url, headers, body):
            calls.append(url.split("/actions")[1])
            if "/workflows/" in url:
                return 200, {}, {"workflow_runs": []}
            return 200, {}, {"id": int(url.rsplit("/", 1)[1]), "status": "completed"}

        cache = ReadCache(transport, clock=lambda: 0)
        observer = GitHub("owner/repo", lambda: "token", cache)
        ops = Operations(self.store, self.workflow, self.reader, cache.invalidate)

        def reads():
            observer.run(234)
            observer.run(235)
            observer.runs(recent=True)

        reads()
        ops.request(self.operator, Command("cancel", self.key, source_run_id=234))
        reads()
        self.assertEqual(calls[3:], ["/runs/234", "/workflows/release.yml/runs?branch=main&event=workflow_dispatch&per_page=100&page=1"])
        del calls[:]
        ops.request(self.operator, Command("deploy", str(uuid4()), profile="demo"))
        ops.review(self.approver, self.store.by_run(234)["operation_id"], "prod-kr", True)
        observer.run(234)
        observer.run(235)
        self.assertEqual(calls, ["/runs/234"])

    def test_cancel_does_not_claim_rollback(self):
        outcome = self.ops.request(self.operator, Command("cancel", self.key, source_run_id=234))
        self.assertEqual(outcome["status"], "accepted")
        self.assertNotIn("restored", outcome)
        with self.assertRaises(Conflict):
            self.ops.request(self.operator, Command("cancel", str(uuid4()), source_run_id=234))
