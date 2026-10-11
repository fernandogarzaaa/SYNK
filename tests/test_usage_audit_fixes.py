"""Regression tests for the 2026-10-11 usage-audit findings.

Finding 1 (CRITICAL): POST /act dropped the TCP connection (curl exit 52)
whenever execution ended without a verification object, because the
handler subscripted None entries in the positional "verifications" list.
Finding 2 (HIGH): /plan's default local path was a hardcoded mock with no
honest labeling; mock-produced plans must carry "mock": true.
"""
import json
import threading
import unittest
import urllib.request
from http.server import HTTPServer

from harness.server import Handler, STATE
from harness.local.model import MockLocalModel, EndpointLocalModel


def _post(port, path, body):
    data = json.dumps(body).encode()
    req = urllib.request.Request(
        "http://127.0.0.1:%d%s" % (port, path), data=data,
        headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=15) as r:
        return r.status, json.loads(r.read().decode())


class TestFirstVerification(unittest.TestCase):
    def test_none_entries_skipped(self):
        self.assertIsNone(Handler._first_verification([None]))
        self.assertIsNone(Handler._first_verification([]))

    def test_first_real_entry_returned(self):
        v = {"claim_id": "c1"}
        self.assertEqual(Handler._first_verification([None, v]), v)
        self.assertEqual(Handler._first_verification([v, None]), v)


class TestMockLabeling(unittest.TestCase):
    def test_mock_is_marked_test_double(self):
        self.assertTrue(MockLocalModel.is_test_double)

    def test_endpoint_is_not_test_double(self):
        self.assertFalse(getattr(EndpointLocalModel, "is_test_double", False))


class TestActHttpLayer(unittest.TestCase):
    """The exact audit repro, at the HTTP layer: /act against an
    unregistered origin (default-deny) must return a typed JSON error,
    never a dropped connection."""

    @classmethod
    def setUpClass(cls):
        cls.srv = HTTPServer(("127.0.0.1", 0), Handler)
        cls.port = cls.srv.server_address[1]
        cls.thread = threading.Thread(target=cls.srv.serve_forever,
                                      daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()
        cls.thread.join(timeout=5)

    def test_act_policy_denial_returns_typed_json(self):
        status, body = _post(self.port, "/act", {
            "actions": [{"tool": "type", "selector": "#email",
                         "text": "inan@example.com"}],
            "tab_id": "t1",
            "page_url": "https://evil.example/",
            "task_id": "audit-repro",
        })
        # Before the fix this raised URLError (connection dropped, no
        # HTTP response at all). Now it must be a clean typed error.
        self.assertEqual(status, 200)
        self.assertEqual(body["transaction_status"], "FAILED")
        self.assertEqual(body["results"][0]["error_code"], "POLICY_DENIED")
        self.assertIsNone(body["verification"])
        self.assertIsNone(body["claim_id"])

    def test_plan_mock_flag_present(self):
        status, body = _post(self.port, "/plan", {
            "goal": "fill the email field", "tab_id": "t1"})
        self.assertEqual(status, 200)
        if body.get("routing") == "local_slm":
            # Default local model is the MockLocalModel test double.
            self.assertTrue(body.get("mock"),
                            "mock-produced plan must carry 'mock': true")


if __name__ == "__main__":
    unittest.main()
