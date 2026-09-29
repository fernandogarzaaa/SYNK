"""Shared scaffolding for live-browser tests (tests/live).

Every live test is SKIPPED when no real browser is available, so the
suite stays green in CI without a browser. With a browser present
(playwright + a Chromium build under ~/.cache/ms-playwright), the tests
exercise the REAL paths: OwnedBrowserRuntime over Playwright/CDP, the
unpacked extension end to end, the CDP WebMCP transport against a
page-advertised navigator.modelContext, and the task scheduler against
the live browser.

Stdlib + harness only. The skip guard must never import playwright at
module scope: CI machines without it must collect this package cleanly.
"""
from __future__ import annotations

import http.server
import glob
import os
import socket
import tempfile
import threading
import time
import unittest
import uuid
from functools import partial
from pathlib import Path

LIVE_DIR = Path(__file__).resolve().parent
FIXTURES = LIVE_DIR / "fixtures"
REPO_ROOT = LIVE_DIR.parent.parent


def browser_available() -> bool:
    """True when a real Chromium can plausibly be launched here.

    SYNK_LIVE_BROWSER=0 (or false/no/off) forces False so CI and
    browserless operators get graceful skips without touching the
    machine's browser install.
    """
    flag = os.environ.get("SYNK_LIVE_BROWSER", "")
    if flag.lower() in ("0", "false", "no", "off"):
        return False
    try:
        import playwright  # noqa: F401
    except ImportError:
        return False
    base = os.path.expanduser("~/.cache/ms-playwright")
    # Any downloaded Chromium build (globbed, not a hard-coded version).
    try:
        if glob.glob(os.path.join(base, "chromium-*")):
            return True
    except OSError:
        pass
    # Fall back to a raw executable probe: playwright can also drive a
    # system chrome via executable_path (not used by the harness, but the
    # probe keeps the guard honest).
    for exe in ("chromium", "chromium-browser", "google-chrome",
                "google-chrome-stable"):
        for d in ("/usr/bin", "/usr/local/bin", "/opt/google/chrome"):
            if os.path.isfile(os.path.join(d, exe)):
                return True
    return False


def live_profile_dir(prefix: str) -> str:
    """Fresh Chromium profile dir on the workspace disk, not /tmp.

    /tmp is a small tmpfs in this environment and fills up; Chromium
    profiles (and headful Chrome's temp files) belong on the roomy
    workspace volume.
    """
    base = Path.home() / "workspace" / "tmp" / "synk-live-profiles"
    base.mkdir(parents=True, exist_ok=True)
    return tempfile.mkdtemp(prefix=prefix, dir=str(base))


def container_chrome_args() -> list:
    """Extra Chromium flags for containerized test runs.

    Running as root (typical in CI containers) requires --no-sandbox;
    small /dev/shm needs --disable-dev-shm-usage. Mirrors what an
    operator would pass via OwnedBrowserRuntime.launch(extra_args=...).
    """
    try:
        if os.geteuid() == 0:
            return ["--no-sandbox", "--disable-dev-shm-usage"]
    except AttributeError:
        pass
    return []


def skip_unless_browser(reason: str = "no live browser available"):
    return unittest.skipUnless(browser_available(), reason)


class FixtureServer:
    """Threaded HTTP server over tests/live/fixtures on 127.0.0.1."""

    def __init__(self):
        self._server = None
        self._thread = None
        self.port = 0

    def start(self) -> str:
        # Subclass to silence request logging (tests assert; they don't
        # narrate). Setting log_message on a functools.partial would not
        # take effect; a real subclass does.
        class _Quiet(http.server.SimpleHTTPRequestHandler):
            def log_message(self, *a, **k):
                pass
        handler = partial(_Quiet, directory=str(FIXTURES))
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.bind(("127.0.0.1", 0))
        self.port = sock.getsockname()[1]
        sock.close()
        self._server = http.server.ThreadingHTTPServer(
            ("127.0.0.1", self.port), handler)
        self._thread = threading.Thread(target=self._server.serve_forever,
                                        daemon=True,
                                        name="synk-live-fixtures")
        self._thread.start()
        return self.base_url

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    def stop(self):
        if self._server is not None:
            self._server.shutdown()
            self._server.server_close()
        if self._thread is not None:
            self._thread.join(timeout=5)
        self._server = None
        self._thread = None


# -- engine wiring ------------------------------------------------------------
# The TransactionEngine takes any `tools` object with
# run(action, page_url, user_consented, tab_id=...). LiveToolExecutor runs
# the REAL safety validation, then dispatches through the REAL
# OwnedBrowserRuntime via the same _dispatch_runtime the server's CDP
# path uses -- without needing the HTTP server.


def make_live_engine(runtime):
    """TransactionEngine wired to a live OwnedBrowserRuntime."""
    from harness.world_state import WorldState
    from harness.context_manager import ContextManager
    from harness.concurrency import OwnershipGraph, LeaseManager
    from harness.safety import SafetyLayer
    from harness.verification.verifier import Verifier
    from harness.transactions import TransactionEngine

    world = WorldState()
    ctx = ContextManager()
    verifier = Verifier(world)
    safety = SafetyLayer()
    tools = LiveToolExecutor(runtime, safety)
    ownership = OwnershipGraph()
    leases = LeaseManager(ownership)
    engine = TransactionEngine(world, ownership, leases,
                               tools, safety, ctx, verifier)
    # Expose the lease manager for scheduler tests: the scheduler must
    # share the engine's ownership/lease state, not a fresh instance.
    engine.live_leases = leases
    # The fixture server is a local test origin: allow the full action
    # surface so policy is exercised as "allowed", not bypassed.
    engine.policy.register_origin(
        "127.0.0.1", allow=["read", "navigate", "interact", "write"],
        description="live-test fixture server")
    return engine


class LiveToolExecutor:
    """Dispatch engine actions through a real BrowserRuntime.

    Not a ToolExecutor subclass: the CDP branch of ToolExecutor.run is
    coupled to server STATE (loop + browser registry). This class keeps
    the honest parts -- schema allowlist, SafetyLayer validation, and
    the shared _dispatch_runtime primitive mapping -- and takes the
    runtime explicitly.
    """

    def __init__(self, runtime, safety=None):
        from harness.safety import SafetyLayer
        self.live_runtime = runtime
        self.safety = safety or SafetyLayer()
        self.paused_for_user = False

    def run(self, action, page_url="", user_consented=False,
            tab_id="default"):
        from harness.tools import TOOL_SCHEMAS, _dispatch_runtime
        from harness.browser_runtime import ElementTarget
        from harness.session import MAIN_FRAME
        tool = action.get("tool", action.get("action", ""))
        if tool not in TOOL_SCHEMAS:
            return {"ok": False, "error": "denied:unknown-tool"}
        ok, reason = self.safety.validate(
            action, page_url, user_consented, self.paused_for_user)
        if not ok:
            return {"ok": False, "error": reason}
        selector = action.get("selector") or action.get("target") or ""
        target = ElementTarget(
            session_id=action.get("session_id"),
            window_id=action.get("window_id", "win_default"),
            tab_id=tab_id, frame_id=action.get("frame_id", "main"),
            frame_chain=[MAIN_FRAME], shadow_path=[],
            locator={"strategy": "css", "value": selector})
        try:
            return _dispatch_runtime(self.live_runtime, tool, target,
                                     action, tab_id)
        except Exception as e:
            return {"ok": False,
                    "error": f"live dispatch failed: {type(e).__name__}: {e}"}


# -- independent observation --------------------------------------------------
# The central invariant, exercised live: after an action runs, a FRESH
# runtime.snapshot() (never the action's own ack) is read, and the
# reading is recorded as ELEMENT_STATE/DOM_CHANGE evidence. Only then
# may the claim verify.


def find_node(nodes, selector: str):
    for n in nodes or []:
        if n.get("selector") == selector:
            return n
    return None


def record_independent_observation(verifier, *, evidence_type: str,
                                   action_id: str, task_id: str,
                                   payload: dict):
    """Record a live independent observation as verifier evidence."""
    from harness.verification.evidence import Evidence
    payload = dict(payload)
    payload.setdefault(
        "provenance_note",
        "live-test: fresh runtime.snapshot() read AFTER the action, "
        "independent of the action's own ack")
    verifier.record_evidence(Evidence(
        evidence_id=f"e_live_{uuid.uuid4().hex[:8]}",
        evidence_type=evidence_type,
        source="runtime", timestamp=time.time(),
        action_id=action_id, task_id=task_id,
        payload=payload, provenance="runtime"))


def wait_until(fn, timeout: float = 20.0, interval: float = 0.5,
               desc: str = "condition"):
    """Poll fn() until truthy; raise AssertionError on timeout."""
    deadline = time.time() + timeout
    last = None
    while time.time() < deadline:
        try:
            last = fn()
        except Exception as e:  # keep polling through transient errors
            last = e
        if last and not isinstance(last, Exception):
            return last
        time.sleep(interval)
    raise AssertionError(f"timed out waiting for: {desc} (last={last!r})")
