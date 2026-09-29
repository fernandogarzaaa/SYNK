"""Live verification: owned-launch mode + the central invariant.

Spins a REAL Chromium via OwnedBrowserRuntime.launch().connect(), drives
the fixture checkout page through the REAL TransactionEngine (safety ->
policy -> leases -> _dispatch_runtime -> Playwright), and verifies each
claim ONLY from an independent fresh observation.

Negative control first: right after dispatch, with only the
command-accepted BROWSER_EVENT recorded, the claim MUST be UNVERIFIED.
Then the independent observation is recorded and the claim MUST become
VERIFIED. This exercises the shipped invariant live; it is not a
paraphrase of a unit test.
"""
import os
import shutil
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from tests.live.helpers import (
    FixtureServer, browser_available, container_chrome_args,
    find_node, live_profile_dir, make_live_engine,
    record_independent_observation, skip_unless_browser, wait_until,
)


@skip_unless_browser("live owned-launch test needs a real browser")
class TestLiveOwnedLaunch(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from harness.browser_owned import OwnedBrowserRuntime
        cls.server = FixtureServer()
        cls.base_url = cls.server.start()
        cls.checkout_url = cls.base_url + "/checkout.html"
        cls.plain_url = cls.base_url + "/plain.html"
        cls.profile_dir = live_profile_dir(prefix="synk-live-owned-")
        cls.runtime = OwnedBrowserRuntime.launch(
            profile_dir=cls.profile_dir, headless=True,
            extra_args=container_chrome_args())
        cls.info = cls.runtime.connect()
        cls.tab = cls.runtime.new_tab(cls.checkout_url)
        cls.engine = make_live_engine(cls.runtime)

    @classmethod
    def tearDownClass(cls):
        try:
            cls.runtime.disconnect()
        finally:
            cls.server.stop()
            shutil.rmtree(cls.profile_dir, ignore_errors=True)

    def setUp(self):
        # Every test starts from a fresh checkout page: reload, then wait
        # for the form to be interactable again.
        self.runtime.navigate(self.checkout_url, tab_id=self.tab)
        wait_until(
            lambda: find_node(
                self.runtime.snapshot(self.tab)["nodes"], "#email"),
            desc="checkout form present after reload")

    # -- helpers ---------------------------------------------------------
    def _execute(self, actions, task_id):
        return self.engine.execute(
            actions, task_id=task_id, tab_id=self.tab,
            page_url=self.checkout_url)

    def _observe_value(self, selector):
        snap = self.runtime.snapshot(self.tab)
        node = find_node(snap["nodes"], selector)
        self.assertIsNotNone(node, f"no node {selector} in fresh snapshot")
        return node

    # -- the invariant, live ---------------------------------------------
    def test_type_verified_only_by_independent_observation(self):
        from harness.verification.evidence import ELEMENT_STATE
        report = self._execute(
            [{"tool": "type", "selector": "#email",
              "text": "live@example.com"}],
            task_id="t_live_type")
        ex = report.executions[0]
        # Negative control: the executor accepted the command (ack with
        # observation), but no INDEPENDENT observation exists yet, so the
        # claim must NOT verify. This is the shipped invariant, live.
        self.assertEqual(ex.verification["result"], "UNVERIFIED")
        self.assertIn("command-accepted evidence is never sufficient",
                      ex.verification["reason"])

        # Independent re-observation: a FRESH snapshot, never the ack.
        node = self._observe_value("#email")
        self.assertEqual(node.get("value"), "live@example.com")

        record_independent_observation(
            self.engine.verifier, evidence_type=ELEMENT_STATE,
            action_id=ex.request.action_id, task_id="t_live_type",
            payload={"target": "#email", "value": node.get("value")})
        result = self.engine.verifier.verify(ex.claim_id)
        self.assertEqual(result.result, "VERIFIED")
        self.assertIn("independent observation confirms", result.reason)

    def test_click_submit_verified_by_confirmation(self):
        # APPLICATION_CONFIRMATION is the semantically right evidence for
        # "the app confirmed the outcome" (a DOM_CHANGE carrying a freeform
        # state string would trip the verifier's scalar conflict check
        # against the click claim's claimed_state -- correctly).
        from harness.verification.evidence import APPLICATION_CONFIRMATION
        self._execute(
            [{"tool": "type", "selector": "#email", "text": "a@b.c"},
             {"tool": "type", "selector": "#addr", "text": "123 Main St"}],
            task_id="t_live_fill")
        report = self._execute(
            [{"tool": "click", "selector": "#submit"}],
            task_id="t_live_submit")
        ex = report.executions[0]
        self.assertEqual(ex.verification["result"], "UNVERIFIED")

        # Independent re-observation: the confirmation div really appeared.
        def confirmation():
            node = self._observe_value("#confirmation")
            if node.get("visible") and "Order confirmed" in (
                    node.get("name") or ""):
                return node
            return None
        node = wait_until(confirmation, desc="confirmation visible")
        record_independent_observation(
            self.engine.verifier, evidence_type=APPLICATION_CONFIRMATION,
            action_id=ex.request.action_id, task_id="t_live_submit",
            payload={"target": "#submit",
                     "detail": "confirmation visible: "
                               + (node.get("name") or "")})
        result = self.engine.verifier.verify(ex.claim_id)
        self.assertEqual(result.result, "VERIFIED")

    def test_navigate_verified_by_url(self):
        from harness.verification.evidence import URL_CHANGE
        report = self.engine.execute(
            [{"tool": "navigate", "url": self.plain_url}],
            task_id="t_live_nav", tab_id=self.tab,
            page_url=self.checkout_url)
        ex = report.executions[0]
        self.assertEqual(ex.verification["result"], "UNVERIFIED")

        observed = self.runtime.observe(tab_id=self.tab)
        self.assertEqual(observed.url, self.plain_url)
        record_independent_observation(
            self.engine.verifier, evidence_type=URL_CHANGE,
            action_id=ex.request.action_id, task_id="t_live_nav",
            payload={"url": observed.url, "tab_id": self.tab})
        result = self.engine.verifier.verify(ex.claim_id)
        self.assertEqual(result.result, "VERIFIED")
        self.assertIn("independent URL observation matches", result.reason)

    def test_contradicting_observation_does_not_verify(self):
        # The verifier must compare, not rubber-stamp: record an
        # observation that contradicts the typed value and confirm the
        # claim stays UNVERIFIED.
        from harness.verification.evidence import ELEMENT_STATE
        report = self._execute(
            [{"tool": "type", "selector": "#email", "text": "a@b.c"}],
            task_id="t_live_contra")
        ex = report.executions[0]
        record_independent_observation(
            self.engine.verifier, evidence_type=ELEMENT_STATE,
            action_id=ex.request.action_id, task_id="t_live_contra",
            payload={"target": "#email", "value": "something-else"})
        result = self.engine.verifier.verify(ex.claim_id)
        self.assertEqual(result.result, "UNVERIFIED")
        self.assertIn("no observation confirms the expected value",
                      result.reason)

    def test_ack_carries_observation(self):
        # BrowserAck invariant, live: executed=True is inseparable from
        # the post-action observation.
        from harness.browser_runtime import ElementTarget
        from harness.session import MAIN_FRAME
        target = ElementTarget(
            session_id=None, window_id="win_default", tab_id=self.tab,
            frame_id="main", frame_chain=[MAIN_FRAME], shadow_path=[],
            locator={"strategy": "css", "value": "#email"})
        ack = self.runtime.type(target, "ack@example.com")
        self.assertTrue(ack.executed)
        self.assertIsNotNone(ack.observed)
        self.assertEqual(ack.observed.tab_id, self.tab)
        node = self._observe_value("#email")
        self.assertEqual(node.get("value"), "ack@example.com")


@skip_unless_browser("live attach test needs a real browser")
class TestLiveAttachMode(unittest.TestCase):
    """Attach to an operator-started --remote-debugging-port endpoint."""

    @classmethod
    def setUpClass(cls):
        import json as _json
        import subprocess as _sp
        import urllib.request as _url
        base = os.path.expanduser("~/.cache/ms-playwright")
        cls.chrome = os.path.join(base, "chromium-1243",
                                  "chrome-linux64", "chrome")
        if not os.path.isfile(cls.chrome):
            raise unittest.SkipTest("no chromium binary for attach test")
        cls.user_data = live_profile_dir(prefix="synk-live-attach-")
        cls.port_file = os.path.join(cls.user_data, "DevToolsActivePort")
        cmd = [cls.chrome, "--headless=new", "--no-sandbox",
               "--disable-dev-shm-usage", "--remote-debugging-port=0",
               "--user-data-dir=" + cls.user_data, "about:blank"]
        cls.proc = _sp.Popen(cmd, stdout=_sp.DEVNULL, stderr=_sp.DEVNULL)
        port = wait_until(
            lambda: (open(cls.port_file).read().splitlines()[0]
                     if os.path.exists(cls.port_file) else None),
            timeout=30, desc="DevToolsActivePort file")
        with _url.urlopen(
                f"http://127.0.0.1:{port}/json/version",
                timeout=10) as r:
            cls.ws_url = _json.loads(r.read().decode())[
                "webSocketDebuggerUrl"]
        assert cls.ws_url.startswith("ws"), cls.ws_url

    @classmethod
    def tearDownClass(cls):
        try:
            cls.proc.terminate()
            cls.proc.wait(timeout=10)
        finally:
            shutil.rmtree(cls.user_data, ignore_errors=True)

    def test_attach_navigate_observe(self):
        from harness.browser_owned import OwnedBrowserRuntime
        rt = OwnedBrowserRuntime.attach(self.ws_url)
        try:
            info = rt.connect()
            self.assertEqual(info["mode"], "attached-endpoint")
            self.assertEqual(rt.mode, "attached-endpoint")
            server = FixtureServer()
            base = server.start()
            try:
                tab = rt.new_tab(base + "/plain.html")
                obs = rt.observe(tab_id=tab)
                self.assertEqual(obs.url, base + "/plain.html")
                tabs = rt.list_tabs()
                self.assertTrue(any(t["tab_id"] == tab for t in tabs))
            finally:
                server.stop()
        finally:
            rt.disconnect()

    def test_attach_rejects_non_ws_endpoint(self):
        from harness.browser_owned import OwnedBrowserRuntime
        with self.assertRaises(ValueError):
            OwnedBrowserRuntime.attach("http://127.0.0.1:9999/json")
