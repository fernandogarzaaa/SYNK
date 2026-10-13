"""Cross-site / DNS-rebinding guard for the unauthenticated loopback harness.

Before this guard, any web page open in the user's browser could send a
"simple" text/plain POST (no CORS preflight) to http://127.0.0.1:18080 and
register itself in the default-deny origin policy, drive /act or /transact,
or read /memory and /audit (responses carried Access-Control-Allow-Origin: *).
Only the content-script push endpoints (/snapshot, /event) stay open to web
origins.
"""
import json
import threading
import unittest
import urllib.error
import urllib.request
from http.server import HTTPServer

from harness.server import Handler, STATE, check_request, origin_is_trusted


def _req(port, path, body=None, headers=None, method=None):
    data = None if body is None else json.dumps(body).encode()
    req = urllib.request.Request(
        "http://127.0.0.1:%d%s" % (port, path), data=data,
        headers=headers or {}, method=method)
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(req, timeout=15) as r:
            return r.status, dict(r.headers), r.read().decode()
    except urllib.error.HTTPError as e:
        return e.code, dict(e.headers), e.read().decode()


class TestCheckRequest(unittest.TestCase):
    def test_no_origin_is_local_client(self):
        self.assertEqual(check_request("127.0.0.1:18080", None, "/act", "POST"), (True, ""))
        self.assertEqual(check_request(None, None, "/memory", "GET"), (True, ""))

    def test_web_origin_blocked_off_push_paths(self):
        for path in ("/act", "/transact", "/policy/origin", "/memory/forget"):
            self.assertEqual(
                check_request("127.0.0.1:18080", "https://evil.example", path, "POST"),
                (False, "cross_origin_forbidden"), path)
        self.assertFalse(check_request("127.0.0.1:18080", "https://evil.example", "/memory", "GET")[0])
        self.assertFalse(check_request("127.0.0.1:18080", "null", "/act", "POST")[0])

    def test_web_origin_allowed_on_push_paths(self):
        for path in ("/snapshot", "/event"):
            self.assertTrue(check_request("127.0.0.1:18080", "https://news.example", path, "POST")[0])
            self.assertTrue(check_request("127.0.0.1:18080", "https://news.example", path, "OPTIONS")[0])
        # GET on a push path is not part of the content-script contract
        self.assertFalse(check_request("127.0.0.1:18080", "https://news.example", "/snapshot", "GET")[0])

    def test_rebinding_host_rejected(self):
        self.assertEqual(check_request("evil.example:18080", None, "/audit", "GET"),
                         (False, "host_not_allowed"))
        self.assertTrue(check_request("localhost:18080", None, "/audit", "GET")[0])
        self.assertTrue(check_request("[::1]:18080", None, "/audit", "GET")[0])

    def test_trusted_origins(self):
        for o in ("chrome-extension://abcdefghijklmnop", "moz-extension://x",
                  "tauri://localhost", "http://tauri.localhost",
                  "http://localhost:5173", "http://127.0.0.1:1420"):
            self.assertTrue(origin_is_trusted(o), o)
        for o in ("https://evil.example", "http://localhost.evil.example",
                  "http://127.0.0.1.evil.example", "null", ""):
            self.assertFalse(origin_is_trusted(o), o)


class TestGuardHttpLayer(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.srv = HTTPServer(("127.0.0.1", 0), Handler)
        cls.port = cls.srv.server_address[1]
        cls.thread = threading.Thread(target=cls.srv.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()
        cls.thread.join(timeout=5)

    def test_cross_site_policy_registration_blocked(self):
        origin = "https://evil-guard-test.example"
        status, headers, body = _req(
            self.port, "/policy/origin",
            {"origin": origin, "allow": ["click", "type"]},
            {"Content-Type": "text/plain", "Origin": origin})
        self.assertEqual(status, 403)
        self.assertEqual(json.loads(body)["error"], "cross_origin_forbidden")
        self.assertNotIn("Access-Control-Allow-Origin", headers)
        self.assertNotIn(origin, STATE.policy.snapshot())

    def test_cross_site_memory_read_blocked(self):
        status, headers, _ = _req(self.port, "/memory",
                                  headers={"Origin": "https://evil.example"})
        self.assertEqual(status, 403)
        self.assertNotIn("Access-Control-Allow-Origin", headers)

    def test_rebinding_host_blocked(self):
        status, _, body = _req(self.port, "/audit",
                               headers={"Host": "evil.example:%d" % self.port})
        self.assertEqual(status, 403)
        self.assertEqual(json.loads(body)["error"], "host_not_allowed")

    def test_content_script_push_still_works(self):
        status, headers, _ = _req(
            self.port, "/event", {"type": "guard.test", "data": {}},
            {"Content-Type": "application/json", "Origin": "https://news.example"})
        self.assertEqual(status, 200)
        self.assertEqual(headers.get("Access-Control-Allow-Origin"), "*")
        status, headers, _ = _req(
            self.port, "/snapshot", method="OPTIONS",
            headers={"Origin": "https://news.example",
                     "Access-Control-Request-Method": "POST"})
        self.assertEqual(status, 204)
        self.assertEqual(headers.get("Access-Control-Allow-Origin"), "*")

    def test_extension_origin_reflected(self):
        o = "chrome-extension://abcdefghijklmnop"
        status, headers, _ = _req(self.port, "/memory", headers={"Origin": o})
        self.assertEqual(status, 200)
        self.assertEqual(headers.get("Access-Control-Allow-Origin"), o)

    def test_local_client_without_origin_unchanged(self):
        status, headers, _ = _req(self.port, "/health")
        self.assertEqual(status, 200)
        self.assertNotIn("Access-Control-Allow-Origin", headers)


if __name__ == "__main__":
    unittest.main()
