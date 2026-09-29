"""Live verification: unpacked-extension snapshot flow.

Starts the REAL harness server (harness.server, port 18080, in-process
so the test can read its session/context state), launches a REAL
headful Chromium under Xvfb with the UNPACKED extension
(repo: extension/) loaded via --load-extension, opens the fixture
checkout page, and waits until the extension's content script pushes a
/snapshot that the harness ingests: the tab is registered in
STATE.sessions and the nodes land in the context manager's tab
snapshot. This is the attached-extension adapter's live path -- no
fake transport.
"""
import os
import shutil
import sys
import threading
import unittest
from http.server import HTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from tests.live.helpers import (
    FixtureServer, container_chrome_args, live_profile_dir,
    skip_unless_browser, wait_until,
)

EXT_DIR = str(Path(__file__).resolve().parent.parent.parent / "extension")
HARNESS_PORT = 18080


def _start_harness_server():
    import harness.server as srv
    srv.STATE = srv.State(":memory:")
    srv.STATE._human_steps = []
    httpd = HTTPServer(("127.0.0.1", HARNESS_PORT), srv.Handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    return srv, httpd


@skip_unless_browser("live extension test needs a real browser")
class TestLiveExtensionSnapshot(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if not os.environ.get("DISPLAY"):
            os.environ["DISPLAY"] = ":99"
        # Headful Chrome writes crashpad/shm temp files; /tmp is a small
        # tmpfs in this environment, so point TMPDIR at workspace disk.
        os.environ["TMPDIR"] = str(Path.home() / "workspace" / "tmp")
        from harness.browser_owned import OwnedBrowserRuntime
        cls.server = FixtureServer()
        cls.base_url = cls.server.start()
        cls.checkout_url = cls.base_url + "/checkout.html"
        cls.srv, cls.httpd = _start_harness_server()
        wait_until(
            lambda: _health_ok(), timeout=15,
            desc="harness server /health")
        cls.profile_dir = live_profile_dir(prefix="synk-live-ext-")
        cls.runtime = OwnedBrowserRuntime.launch(
            profile_dir=cls.profile_dir, headless=False,
            # Playwright disables extensions by default; the operator
            # explicitly re-enables them to load this unpacked extension.
            ignore_default_args=["--disable-extensions"],
            extra_args=container_chrome_args() + [
                "--load-extension=" + EXT_DIR,
                "--disable-extensions-except=" + EXT_DIR,
                "--window-size=1280,900",
            ])
        cls.runtime.connect()
        cls.tab = cls.runtime.new_tab(cls.checkout_url)

    @classmethod
    def tearDownClass(cls):
        try:
            cls.runtime.disconnect()
        finally:
            try:
                cls.httpd.shutdown()
            except Exception:
                pass
            cls.server.stop()
            shutil.rmtree(cls.profile_dir, ignore_errors=True)

    def _ingested(self):
        """Return the harness-side tab snapshot for the fixture page."""
        for snap in self.srv.STATE.ctx._tab_snapshots.values():
            if "checkout.html" in (snap.get("url") or ""):
                return snap
        return None

    def test_extension_push_ingested_by_harness(self):
        snap = wait_until(self._ingested, timeout=90,
                          desc="extension snapshot ingested")
        self.assertTrue(snap["nodes"],
                        "harness ingested an empty node list")
        selectors = {n.get("selector") for n in snap["nodes"]}
        self.assertIn("#email", selectors)
        self.assertIn("#submit", selectors)
        # The session registry also knows the tab (canonical session
        # registration path in _snapshot).
        sessions = self.srv.STATE.sessions.snapshot()["sessions"]
        urls = [t.get("url", "")
                for s in sessions.values()
                for w in s.get("windows", {}).values()
                for t in w.get("tabs", {}).values()]
        self.assertTrue(any("checkout.html" in u for u in urls),
                        f"no checkout tab registered: {urls}")

    def test_extension_reports_input_values(self):
        # The extension does not re-push snapshots on input; it reports
        # a value.changed event per keystroke run (sendEvent -> /event).
        # Type through the owned runtime (real input DOM events) and
        # assert the harness event bus received the live value.
        from harness.browser_runtime import ElementTarget
        from harness.session import MAIN_FRAME
        target = ElementTarget(
            session_id=None, window_id="win_default", tab_id=self.tab,
            frame_id="main", frame_chain=[MAIN_FRAME], shadow_path=[],
            locator={"strategy": "css", "value": "#email"})
        self.runtime.type(target, "ext-push@example.com")

        def value_event():
            for ev in self.srv.STATE.bus.events:
                data = ev.get("data") or {}
                if ev.get("type") == "value.changed" and \
                        data.get("target") == "#email" and \
                        data.get("value") == "ext-push@example.com":
                    return ev
            return None
        ev = wait_until(value_event, timeout=60,
                        desc="value.changed event with typed value")
        self.assertIn("checkout.html", (ev["data"] or {}).get("url", ""))


def _health_ok():
    import urllib.request
    try:
        with urllib.request.urlopen(
                f"http://127.0.0.1:{HARNESS_PORT}/health",
                timeout=3) as r:
            return r.status == 200
    except Exception:
        return None
