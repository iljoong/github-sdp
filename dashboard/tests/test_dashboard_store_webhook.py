import hashlib
import hmac
import json
import unittest

from dashboard.config import SharedSettings
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

    def test_github_run_ids_above_int32_round_trip_through_table(self):
        run_id = 19123456789
        calls = []

        class SDKBoundary:
            def submit_transaction(self, changes):
                calls.extend(changes)

            def query_entities(self, query, parameters):
                if parameters["id"] == "19123456789":
                    return iter([{"run_id": "19123456789"}])
                return iter([])

        table = OperationsTable(SDKBoundary(), "23")
        table.reserve("unique", {"source_run_id": run_id})
        self.assertEqual(calls[0][1]["source_run_id"], "19123456789")
        self.assertEqual(table.by_run(run_id)["run_id"], run_id)
