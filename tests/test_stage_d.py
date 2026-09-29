"""Stage D regression tests: the real browser layer.

Run: python -m unittest discover -s tests -v   (also collected by pytest)

Coverage:
- BrowserRuntime adapter contract against a deterministic in-memory fake
- BrowserAck invariant: executed=True is inseparable from an observation
- Interaction message + ack parsing (build_execute_message, parse_ack_payload,
  ack_for_observation, ack_not_executed)
- INTERACTION_JS primitive characteristics: native value setter for
  controlled inputs, contenteditable, select, checkbox/radio, keyboard
  events, shadow-root traversal, post-state observation
- Frame-chain and shadow-path ref resolution; fail-closed drift
- Integer refs never reach the browser adapter as a selector
- Launch-vs-attach mode distinction; absent Playwright -> clear error
- Crash/restart logic and per-tab health transitions (fake backend)
- Attached-extension adapter never claims launch/CDP; owned adapter never
  claims to be the user's browser
- BROWSER_ACK evidence: validated, recorded, audit-trail only; dishonest
  acks fail closed
- Frame identity: parent/chain, document preserved across snapshots,
  replaced on navigation
- Extension JS syntax (node --check)

No real Chrome/CDP is exercised here: browser behavior is validated through
the deterministic fake backend plus syntax and contract checks. Real-browser
integration of the Playwright adapter is explicitly unverified in this
environment.
"""
import shutil
import subprocess
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from harness.browser_runtime import (
    BrowserRuntime, BrowserAck, BrowserObservation, ElementTarget,
    TabHealth, HEALTHY, DEGRADED, UNRESPONSIVE, CRASHED, CLOSED,
    TAB_HEALTH_STATES, ElementNotFound, AmbiguousElement,
    UnsupportedOperation, BrowserRuntimeError, observation_id_for,
)
from harness.interactions import (
    INTERACTION_JS, build_execute_message, parse_ack_payload,
    ack_for_observation, ack_not_executed,
)
from harness.ref_resolver import resolve_action_target, RefResolutionError
from harness.context_manager import ContextManager
from harness.session import (
    SessionManager, MAIN_FRAME, stable_id,
)
from harness.verification.evidence import (
    BROWSER_ACK, BROWSER_EVENT, satisfies_postcondition, strength_of,
    EVIDENCE_STRENGTH,
)
from harness.browser_extension import ExtensionBrowserRuntime
from harness.browser_owned import OwnedBrowserRuntime, DEFAULT_PROFILE_DIR


# ---------------------------------------------------------------------------
# Deterministic in-memory fake backend implementing the full contract.
# ---------------------------------------------------------------------------
class FakeBrowserRuntime(BrowserRuntime):
    mode = "fake"

    def __init__(self):
        self._connected = False
        self.tabs = {
            "t1": {"url": "https://example.com", "title": "Example",
                   "elements": {"#name": {"tag": "input", "type": "text",
                                          "value": ""},
                                "#go": {"tag": "button"}}},
        }
        self.health = {"t1": TabHealth(tab_id="t1", state=HEALTHY)}
        self.crashed_tabs = set()
        self.received_locators = []  # every locator value handed to the adapter
        self.event_cbs = []

    # -- lifecycle --
    def connect(self):
        self._connected = True
        return {"mode": "fake", "session_id": "s1"}

    def disconnect(self):
        self._connected = False

    @property
    def connected(self):
        return self._connected

    # -- tabs --
    def list_tabs(self, *, window_id=None, session_id=None):
        return [{"tab_id": tid, "window_id": window_id or "win_default",
                 "url": t["url"], "title": t["title"],
                 "health": self.health[tid].state}
                for tid, t in self.tabs.items()]

    def tab_health(self, tab_id):
        return self.health[tab_id]

    # -- observation --
    def _obs(self, tab_id, frame_id="main"):
        t = self.tabs[tab_id]
        return BrowserObservation(
            observation_id=observation_id_for(tab_id, frame_id, t["url"]),
            session_id="s1", window_id="win_default",
            tab_id=tab_id, frame_id=frame_id,
            url=t["url"], title=t["title"], target_state={})

    def observe(self, *, tab_id, frame_id=MAIN_FRAME,
                session_id=None, window_id="win_default"):
        if tab_id in self.crashed_tabs:
            raise BrowserRuntimeError("renderer gone")
        self.health[tab_id].record_success()
        return self._obs(tab_id, frame_id)

    def locate(self, target):
        # Contract probe: a raw ref integer must never arrive here.
        val = target.locator.get("value")
        self.received_locators.append(val)
        if isinstance(val, int):
            raise AssertionError("raw ref integer leaked to the adapter")
        els = self.tabs[target.tab_id]["elements"]
        matches = [k for k in els if k == val]
        if not matches:
            raise ElementNotFound(f"no such element {val!r}")
        if len(matches) > 1:
            raise AmbiguousElement(val)
        return {"found": True, "count": 1,
                "target_state": dict(els[matches[0]])}

    # -- interactions --
    def _ack(self, command, target, action_id, observed_extra=None):
        obs = self._obs(target.tab_id, target.frame_id)
        obs.target_state = dict(observed_extra or {})
        return BrowserAck(
            ack_id="ack_fake", action_id=action_id, session_id="s1",
            window_id="win_default", tab_id=target.tab_id,
            frame_id=target.frame_id, command=command,
            accepted=True, executed=True, observed=obs)

    def click(self, target, *, action_id=None):
        st = self.locate(target)["target_state"]
        return self._ack("click", target, action_id,
                         {"clicked": target.locator.get("value"), **st})

    def type(self, target, text, *, action_id=None, clear_first=True):
        st = self.locate(target)["target_state"]
        if st.get("tag") == "input":
            self.tabs[target.tab_id]["elements"][
                target.locator["value"]]["value"] = text
            st = dict(st, value=text)
        return self._ack("type", target, action_id, st)

    def select(self, target, value, *, action_id=None):
        st = self.locate(target)["target_state"]
        return self._ack("select", target, action_id,
                         {**st, "selected": value})

    def scroll(self, *, tab_id, frame_id=MAIN_FRAME, direction="down",
               amount=600, session_id=None, window_id="win_default",
               action_id=None):
        tgt = ElementTarget(session_id, window_id, tab_id, frame_id)
        return self._ack("scroll", tgt, action_id,
                         {"direction": direction, "amount": amount})

    def keypress(self, target, key, *, action_id=None, tab_id=None,
                 frame_id=MAIN_FRAME, session_id=None,
                 window_id="win_default"):
        tgt = target or ElementTarget(session_id, window_id,
                                      tab_id or "t1", frame_id)
        return self._ack("press_key", tgt, action_id, {"key": key})

    def navigate(self, url, *, tab_id, frame_id=MAIN_FRAME,
                 session_id=None, window_id="win_default",
                 action_id=None, timeout_ms=30000):
        self.tabs[tab_id]["url"] = url
        tgt = ElementTarget(session_id, window_id, tab_id, frame_id)
        return self._ack("navigate", tgt, action_id, {"url": url})

    def wait_for(self, *, tab_id, frame_id=MAIN_FRAME, condition,
                 timeout_ms=10000, session_id=None,
                 window_id="win_default"):
        return self._obs(tab_id, frame_id)

    # -- inspection --
    def screenshot(self, *, tab_id, frame_id=MAIN_FRAME,
                   session_id=None, window_id="win_default"):
        return b"\x89PNG-fake-bytes"

    def inspect_network(self, *, tab_id, session_id=None, since_ts=0.0):
        return []

    def inspect_dialogs(self, *, tab_id, session_id=None):
        return []

    def inspect_downloads(self, *, tab_id, session_id=None):
        return []

    # -- events --
    def on_event(self, callback):
        self.event_cbs.append(callback)
        def _off():
            self.event_cbs.remove(callback)
        return _off

    # crash injection for health/restart tests. A restart is a NEW
    # incarnation: the tab gets a fresh TabHealth record, mirroring
    # OwnedBrowserRuntime.restart() which clears per-tab health.
    def inject_crash(self, tab_id):
        self.crashed_tabs.add(tab_id)
        self.health[tab_id].mark_crashed("injected")
        for cb in self.event_cbs:
            cb("crash", {"tab_id": tab_id})

    def inject_restart(self, tab_id):
        self.crashed_tabs.discard(tab_id)
        self.health[tab_id] = TabHealth(tab_id=tab_id, state=HEALTHY)
        for cb in self.event_cbs:
            cb("tab.opened", {"tab_id": tab_id})

    def restart(self):
        """Mirror of OwnedBrowserRuntime.restart() semantics: only valid
        for the owned-launch mode."""
        if self.mode != "owned-launch":
            raise UnsupportedOperation(
                "restart() valid only in owned-launch mode")
        self.disconnect()
        self.health = {tid: TabHealth(tab_id=tid, state=HEALTHY)
                       for tid in self.tabs}
        self.crashed_tabs.clear()
        return self.connect()


def make_target(tab_id="t1", locator="#go", frame_chain=None,
                shadow_path=None, ref_id=3):
    return ElementTarget(
        session_id="s1", window_id="win_default", tab_id=tab_id,
        frame_id="main", frame_chain=frame_chain or ["main"],
        shadow_path=shadow_path or [],
        locator={"strategy": "css", "value": locator}, ref_id=ref_id)


class TestAdapterContract(unittest.TestCase):
    def setUp(self):
        self.rt = FakeBrowserRuntime()
        self.rt.connect()

    def test_lifecycle(self):
        self.assertTrue(self.rt.connected)
        self.rt.disconnect()
        self.assertFalse(self.rt.connected)

    def test_list_tabs_and_health(self):
        tabs = self.rt.list_tabs()
        self.assertEqual(len(tabs), 1)
        self.assertEqual(tabs[0]["tab_id"], "t1")
        self.assertEqual(tabs[0]["health"], HEALTHY)
        self.assertIn(self.rt.tab_health("t1").state, TAB_HEALTH_STATES)

    def test_observe_is_read_only(self):
        o1 = self.rt.observe(tab_id="t1")
        o2 = self.rt.observe(tab_id="t1")
        self.assertEqual(o1.url, "https://example.com")
        self.assertEqual(o1.tab_id, "t1")
        self.assertEqual(o1.frame_id, "main")

    def test_click_ack_carries_observation(self):
        ack = self.rt.click(make_target(), action_id="a1")
        self.assertTrue(ack.accepted)
        self.assertTrue(ack.executed)
        self.assertIsNotNone(ack.observed)
        self.assertEqual(ack.observed.target_state["clicked"], "#go")
        self.assertEqual(ack.action_id, "a1")

    def test_type_updates_and_observes_value(self):
        tgt = make_target(locator="#name")
        ack = self.rt.type(tgt, "hello", action_id="a2")
        self.assertTrue(ack.executed)
        self.assertEqual(ack.observed.target_state["value"], "hello")

    def test_navigate_changes_url(self):
        ack = self.rt.navigate("https://example.org", tab_id="t1")
        self.assertTrue(ack.executed)
        self.assertEqual(ack.observed.url, "https://example.org")

    def test_locate_fail_closed(self):
        with self.assertRaises(ElementNotFound):
            self.rt.locate(make_target(locator="#nope"))

    def test_screenshot_returns_bytes(self):
        png = self.rt.screenshot(tab_id="t1")
        self.assertIsInstance(png, bytes)
        self.assertTrue(png.startswith(b"\x89PNG"))

    def test_inspection_lists(self):
        self.assertEqual(self.rt.inspect_network(tab_id="t1"), [])
        self.assertEqual(self.rt.inspect_dialogs(tab_id="t1"), [])
        self.assertEqual(self.rt.inspect_downloads(tab_id="t1"), [])

    def test_events_subscription(self):
        seen = []
        off = self.rt.on_event(lambda t, d: seen.append((t, d)))
        self.rt.inject_crash("t1")
        self.assertEqual(seen[0][0], "crash")
        off()
        self.rt.inject_restart("t1")
        self.assertEqual(len(seen), 1)

    def test_wait_for_returns_observation(self):
        obs = self.rt.wait_for(tab_id="t1", condition="load")
        self.assertEqual(obs.tab_id, "t1")


class TestBrowserAckInvariant(unittest.TestCase):
    def _obs(self):
        return BrowserObservation(
            observation_id="obs_x", session_id="s1",
            window_id="win_default", tab_id="t1", frame_id="main",
            url="https://example.com", title="", target_state={})

    def test_executed_requires_observation(self):
        with self.assertRaises(ValueError):
            BrowserAck(ack_id="a", action_id=None, session_id="s1",
                       window_id="win_default", tab_id="t1", frame_id="main",
                       command="click", accepted=True, executed=True,
                       observed=None)

    def test_not_executed_without_observation_ok(self):
        ack = BrowserAck(ack_id="a", action_id=None, session_id="s1",
                         window_id="win_default", tab_id="t1",
                         frame_id="main", command="click", accepted=True,
                         executed=False, observed=None,
                         error="boom", error_code="ACTION_FAILED")
        self.assertFalse(ack.executed)
        self.assertEqual(ack.error_code, "ACTION_FAILED")

    def test_executed_with_observation_ok(self):
        ack = BrowserAck(ack_id="a", action_id="a1", session_id="s1",
                         window_id="win_default", tab_id="t1",
                         frame_id="main", command="click", accepted=True,
                         executed=True, observed=self._obs())
        d = ack.to_dict()
        self.assertTrue(d["executed"])
        self.assertIsNotNone(d["observed"])
        self.assertEqual(d["observed"]["tab_id"], "t1")

    def test_ack_for_observation_is_honest(self):
        tgt = make_target()
        ack = ack_for_observation("click", tgt, {"clicked": "#go"},
                                  "a1", "https://example.com")
        self.assertTrue(ack.executed)
        self.assertEqual(ack.observed.target_state["clicked"], "#go")
        self.assertEqual(ack.observed.tab_id, "t1")

    def test_ack_not_executed_carries_error(self):
        tgt = make_target()
        ack = ack_not_executed("click", tgt, "no such element",
                               "STALE_REFERENCE", action_id="a1")
        self.assertFalse(ack.executed)
        self.assertIsNone(ack.observed)
        self.assertEqual(ack.error_code, "STALE_REFERENCE")


class TestInteractionMessages(unittest.TestCase):
    def test_build_execute_message_shape(self):
        tgt = make_target(frame_chain=["main", "sub:1a"],
                          shadow_path=["#host"])
        msg = build_execute_message("type", tgt, {"text": "hi"},
                                    action_id="a9")
        self.assertEqual(msg["type"], "EXECUTE")
        self.assertEqual(msg["command"], "type")
        self.assertEqual(msg["action_id"], "a9")
        self.assertEqual(msg["target"]["frame_chain"], ["main", "sub:1a"])
        self.assertEqual(msg["target"]["shadow_path"], ["#host"])
        self.assertEqual(msg["target"]["locator"],
                         {"strategy": "css", "value": "#go"})
        self.assertEqual(msg["args"], {"text": "hi"})
        # The raw ref integer is not part of the dispatch message.
        self.assertNotIn("ref", msg["target"])

    def test_parse_ack_round_trip(self):
        raw = {"ack": True, "ack_id": "ack_1", "action_id": "a1",
               "command": "click", "accepted": True, "executed": True,
               "tab_id": "t1", "window_id": "win_default",
               "frame_id": "main", "session_id": "s1",
               "observed": {"observation_id": "obs_1",
                            "url": "https://example.com",
                            "target_state": {"clicked": True},
                            "captured_at": 123.0}}
        ack = parse_ack_payload(raw)
        self.assertTrue(ack.executed)
        self.assertTrue(ack.observed.target_state["clicked"])

    def test_parse_ack_rejects_malformed(self):
        with self.assertRaises(ValueError):
            parse_ack_payload({"nope": True})
        with self.assertRaises(ValueError):
            parse_ack_payload({"ack": True, "executed": True,
                               "observed": "not-a-dict",
                               "tab_id": "t1"})
        # executed=True without observation -> invariant violation
        with self.assertRaises(ValueError):
            parse_ack_payload({"ack": True, "executed": True,
                               "tab_id": "t1"})


class TestInteractionPrimitives(unittest.TestCase):
    """Static characteristics of INTERACTION_JS (the real DOM logic is
    exercised in the browser; here we pin the properties the harness
    depends on so a regression is loud)."""

    def test_native_value_setter_for_controlled_inputs(self):
        self.assertIn("HTMLInputElement.prototype", INTERACTION_JS)
        self.assertIn("HTMLTextAreaElement.prototype", INTERACTION_JS)
        self.assertIn('getOwnPropertyDescriptor(proto, "value")',
                      INTERACTION_JS)

    def test_contenteditable_handling(self):
        self.assertIn("isContentEditable", INTERACTION_JS)
        self.assertIn("insertText", INTERACTION_JS)

    def test_select_handling(self):
        self.assertIn("selectedIndex", INTERACTION_JS)
        self.assertIn("option not found", INTERACTION_JS)

    def test_checkbox_radio_handling(self):
        self.assertIn('"checkbox"', INTERACTION_JS)
        self.assertIn('"radio"', INTERACTION_JS)
        self.assertIn("el.checked", INTERACTION_JS)

    def test_keyboard_event_sequence(self):
        self.assertIn("keydown", INTERACTION_JS)
        self.assertIn("keyup", INTERACTION_JS)
        self.assertIn("KeyboardEvent", INTERACTION_JS)

    def test_shadow_root_traversal(self):
        self.assertIn("shadowRoot", INTERACTION_JS)
        self.assertIn("shadow host not found", INTERACTION_JS)

    def test_frame_targeting_fail_closed(self):
        # The single-responder frame check lives in the content script's
        # EXECUTE handler (it is the browser-side enforcement point).
        src = (Path(__file__).resolve().parent.parent /
               "extension" / "content.js").read_text()
        self.assertIn("not our frame", src)
        self.assertIn("frame_id", src)
        # INTERACTION_JS threads the shadow path through resolution.
        self.assertIn("shadowPath", INTERACTION_JS)

    def test_post_state_observation(self):
        self.assertIn("observeTarget", INTERACTION_JS)
        self.assertIn("observed", INTERACTION_JS)

    def test_ambiguous_locator_fail_closed(self):
        self.assertIn("AMBIGUOUS_ELEMENT", INTERACTION_JS)

    def test_mirror_note_points_at_python_source(self):
        import harness.interactions as mi
        self.assertIn("content.js", mi.__doc__)
        self.assertIn("MIRRORED FROM harness/interactions.py", mi.__doc__)
        src = (Path(__file__).resolve().parent.parent /
               "extension" / "content.js").read_text()
        self.assertIn("MIRRORED FROM", src)
        self.assertIn("harness/interactions.py", src)


class TestRefResolution(unittest.TestCase):
    def setUp(self):
        self.ctx = ContextManager()

    def _ingest(self, nodes, url="https://example.com", tab_id="t1",
                frame_id="main"):
        view = self.ctx.ingest(url, nodes, tab_id=tab_id, frame_id=frame_id)
        return view

    def test_frame_chain_and_shadow_path_resolve(self):
        nodes = [{"role": "textbox", "name": "Name", "tag": "input",
                  "selector": "#name", "frame_id": "sub:aa",
                  "frame_chain": ["main", "sub:aa"],
                  "shadow_path": ["#host"]}]
        view = self._ingest(nodes)
        ref = view["nodes"][0]["ref"]
        action = {"tool": "type", "ref": ref, "text": "x",
                  "frame_chain": ["main", "sub:aa"],
                  "shadow_path": ["#host"]}
        tgt = resolve_action_target(action, self.ctx, tab_id="t1",
                                    frame_id="sub:aa",
                                    page_url="https://example.com")
        self.assertEqual(tgt.frame_chain, ["main", "sub:aa"])
        self.assertEqual(tgt.shadow_path, ["#host"])
        self.assertEqual(tgt.frame_id, "sub:aa")
        self.assertEqual(tgt.locator["value"], "#name")
        self.assertIsInstance(tgt.locator["value"], str)

    def test_frame_chain_drift_fails_closed(self):
        nodes = [{"role": "textbox", "name": "Name", "tag": "input",
                  "selector": "#name", "frame_id": "sub:aa",
                  "frame_chain": ["main", "sub:aa"]}]
        view = self._ingest(nodes)
        ref = view["nodes"][0]["ref"]
        action = {"tool": "type", "ref": ref, "text": "x",
                  "frame_chain": ["main", "sub:bb"]}  # wrong frame
        with self.assertRaises(RefResolutionError):
            resolve_action_target(action, self.ctx, tab_id="t1",
                                  page_url="https://example.com")

    def test_shadow_path_drift_fails_closed(self):
        nodes = [{"role": "textbox", "name": "Name", "tag": "input",
                  "selector": "#name", "shadow_path": ["#host"]}]
        view = self._ingest(nodes)
        ref = view["nodes"][0]["ref"]
        action = {"tool": "type", "ref": ref, "text": "x",
                  "shadow_path": ["#other-host"]}
        with self.assertRaises(RefResolutionError):
            resolve_action_target(action, self.ctx, tab_id="t1",
                                  page_url="https://example.com")

    def test_stale_version_fails_closed(self):
        nodes = [{"role": "button", "name": "Go", "tag": "button",
                  "selector": "#go"}]
        view = self._ingest(nodes)
        ref = view["nodes"][0]["ref"]
        old_version = view["snapshot_version"]
        # A new snapshot invalidates the old ref.
        self._ingest([{"role": "button", "name": "Go", "tag": "button",
                       "selector": "#go"}])
        action = {"tool": "click", "ref": ref, "ref_version": old_version}
        with self.assertRaises(RefResolutionError):
            resolve_action_target(action, self.ctx, tab_id="t1",
                                  page_url="https://example.com")

    def test_unknown_ref_fails_closed(self):
        with self.assertRaises(RefResolutionError):
            resolve_action_target({"tool": "click", "ref": 99999},
                                  self.ctx, tab_id="t1")

    def test_explicit_selector_does_not_need_ref(self):
        tgt = resolve_action_target({"tool": "click", "selector": "#go"},
                                    self.ctx, tab_id="t1")
        self.assertEqual(tgt.locator["value"], "#go")
        self.assertIsNone(tgt.ref_id)

    def test_integer_ref_never_reaches_adapter_as_selector(self):
        """End to end: ref -> ElementTarget -> fake adapter sees a CSS
        string, never the integer."""
        nodes = [{"role": "button", "name": "Go", "tag": "button",
                  "selector": "#go"}]
        view = self._ingest(nodes)
        ref = view["nodes"][0]["ref"]
        self.assertIsInstance(ref, int)
        rt = FakeBrowserRuntime()
        rt.connect()
        action = {"tool": "click", "ref": ref}
        tgt = resolve_action_target(action, self.ctx, tab_id="t1",
                                    page_url="https://example.com")
        ack = rt.click(tgt, action_id="a1")
        self.assertTrue(ack.executed)
        self.assertEqual(rt.received_locators, ["#go"])
        for loc in rt.received_locators:
            self.assertNotIsInstance(loc, int)


class TestModes(unittest.TestCase):
    def test_launch_vs_attach_distinct(self):
        rt1 = OwnedBrowserRuntime.launch(headless=True)
        self.assertEqual(rt1.mode, "owned-launch")
        rt2 = OwnedBrowserRuntime.attach(
            "ws://127.0.0.1:9222/devtools/browser/abc")
        self.assertEqual(rt2.mode, "attached-endpoint")
        self.assertNotEqual(rt1.mode, rt2.mode)

    def test_launch_uses_synk_profile_not_user_data(self):
        rt = OwnedBrowserRuntime.launch(headless=True)
        self.assertIn(".synk", rt._profile_dir)

    def test_attach_requires_explicit_ws_endpoint(self):
        with self.assertRaises(ValueError):
            OwnedBrowserRuntime.attach("http://127.0.0.1:9222/json")
        with self.assertRaises(ValueError):
            OwnedBrowserRuntime.attach("")

    def test_absent_playwright_gives_clear_error(self):
        import harness.browser_owned as bo
        rt = OwnedBrowserRuntime.launch(headless=True)
        real_import = __import__

        def fake_import(name, *a, **k):
            if name.startswith("playwright"):
                raise ImportError("No module named 'playwright'")
            return real_import(name, *a, **k)

        import builtins
        orig = builtins.__import__
        builtins.__import__ = fake_import
        try:
            with self.assertRaises(RuntimeError) as cm:
                rt.connect()
        finally:
            builtins.__import__ = orig
        self.assertIn("playwright", str(cm.exception).lower())

    def test_connect_without_mode_fails(self):
        rt = OwnedBrowserRuntime()
        with self.assertRaises(RuntimeError):
            rt.connect()

    def test_extension_adapter_never_claims_launch_or_cdp(self):
        sessions = SessionManager()
        ctx = ContextManager()
        rt = ExtensionBrowserRuntime(sessions, ctx)
        self.assertEqual(rt.mode, "attached-extension")
        self.assertNotIn("launch", rt.mode)
        self.assertNotIn("cdp", rt.mode)
        # Reads work without any browser launch.
        rt.connect()
        self.assertTrue(rt.connected)
        tabs = rt.list_tabs()
        self.assertIsInstance(tabs, list)
        # Mutations are refused directly: the closed loop owns them.
        tgt = make_target()
        with self.assertRaises(UnsupportedOperation):
            rt.click(tgt)

    def test_owned_adapter_never_claims_user_browser(self):
        # The honest phrasing must be present: managed launch is explicit
        # that it is NOT the user's browser.
        import inspect
        src = inspect.getsource(OwnedBrowserRuntime.launch)
        self.assertIn("never the user's browser", src.lower())
        self.assertIn("No user data is read", src)
        attach_src = inspect.getsource(OwnedBrowserRuntime.attach)
        self.assertIn("operator", attach_src.lower())


class TestHealthAndCrash(unittest.TestCase):
    def setUp(self):
        self.rt = FakeBrowserRuntime()
        self.rt.connect()

    def test_per_tab_health_transitions(self):
        h = TabHealth(tab_id="t1")
        self.assertEqual(h.state, HEALTHY)
        h.record_failure("slow")
        self.assertEqual(h.state, HEALTHY)  # 1 failure: still healthy
        h.record_failure("slow")
        self.assertEqual(h.state, DEGRADED)
        for _ in range(3):
            h.record_failure("slow")
        self.assertEqual(h.state, UNRESPONSIVE)
        h.record_success()
        self.assertEqual(h.state, HEALTHY)
        self.assertEqual(h.consecutive_failures, 0)

    def test_crash_and_restart(self):
        self.rt.inject_crash("t1")
        self.assertEqual(self.rt.tab_health("t1").state, CRASHED)
        with self.assertRaises(BrowserRuntimeError):
            self.rt.observe(tab_id="t1")
        self.rt.inject_restart("t1")
        self.assertEqual(self.rt.tab_health("t1").state, HEALTHY)
        obs = self.rt.observe(tab_id="t1")
        self.assertEqual(obs.tab_id, "t1")

    def test_closed_state(self):
        h = TabHealth(tab_id="t9")
        h.mark_closed()
        self.assertEqual(h.state, CLOSED)
        self.assertIn(CLOSED, TAB_HEALTH_STATES)

    def test_restart_clears_crashed_health(self):
        self.rt.mode = "owned-launch"
        self.rt.inject_crash("t1")
        self.assertEqual(self.rt.tab_health("t1").state, CRASHED)
        self.rt.restart()
        self.assertTrue(self.rt.connected)
        self.assertEqual(self.rt.tab_health("t1").state, HEALTHY)
        obs = self.rt.observe(tab_id="t1")
        self.assertEqual(obs.tab_id, "t1")

    def test_restart_rejected_outside_owned_launch(self):
        rt = FakeBrowserRuntime()
        rt.mode = "attached-endpoint"
        rt.connect()
        with self.assertRaises(UnsupportedOperation):
            rt.restart()

    def test_owned_restart_rejected_for_attach_mode(self):
        import inspect
        src = inspect.getsource(OwnedBrowserRuntime.restart)
        self.assertIn("owned-launch", src)
        self.assertIn("UnsupportedOperation", src)


class TestBrowserAckEvidence(unittest.TestCase):
    def test_ack_evidence_type_ranking(self):
        self.assertGreater(EVIDENCE_STRENGTH[BROWSER_ACK],
                           EVIDENCE_STRENGTH[BROWSER_EVENT])
        # Audit trail only: can never satisfy a postcondition.
        for kind in ("element_value", "element_interaction", "url"):
            self.assertFalse(satisfies_postcondition(BROWSER_ACK, kind))

    def _loop(self):
        from harness.orchestrator import AgentLoop
        from harness.world_state import WorldState
        from harness.verification.verifier import Verifier
        from types import SimpleNamespace
        state = SimpleNamespace(verifier=Verifier(WorldState()))
        loop = AgentLoop.__new__(AgentLoop)
        loop.state = state
        return loop

    def _ctx(self):
        from types import SimpleNamespace
        notes = []

        def note(kind, detail):
            notes.append((kind, detail))
        return SimpleNamespace(task_id="t1", note=note, notes=notes,
                               consecutive_failures=0)

    def _ack(self, executed=True, with_observation=True, action_id="a1"):
        obs = ({"observation_id": "obs_1", "url": "https://example.com",
                "target_state": {"clicked": True},
                "captured_at": 1.0} if with_observation else None)
        return {"ack": True, "ack_id": "ack_1", "action_id": action_id,
                "command": "click", "accepted": True, "executed": executed,
                "observed": obs, "error": None, "error_code": None,
                "tab_id": "t1", "window_id": "win_default",
                "frame_id": "main"}

    def test_honest_ack_recorded_as_evidence(self):
        loop = self._loop()
        ctx = self._ctx()
        err = loop._validate_and_record_ack(ctx, "a1", "c1",
                                            self._ack())
        self.assertIsNone(err)
        evs = list(loop.state.verifier.evidence_store.values())
        self.assertEqual(len(evs), 1)
        self.assertEqual(evs[0].evidence_type, BROWSER_ACK)
        self.assertEqual(evs[0].action_id, "a1")
        self.assertEqual(evs[0].provenance, "executor_ack")

    def test_dishonest_ack_rejected(self):
        loop = self._loop()
        ctx = self._ctx()
        err = loop._validate_and_record_ack(
            ctx, "a1", "c1", self._ack(executed=True,
                                      with_observation=False))
        self.assertIsNotNone(err)
        self.assertIn("dishonest", err)
        self.assertEqual(
            len(loop.state.verifier.evidence_store), 0)

    def test_ack_action_id_mismatch_rejected(self):
        loop = self._loop()
        ctx = self._ctx()
        err = loop._validate_and_record_ack(
            ctx, "a1", "c1", self._ack(action_id="WRONG"))
        self.assertIsNotNone(err)
        self.assertIn("mismatch", err)

    def test_malformed_ack_rejected(self):
        loop = self._loop()
        ctx = self._ctx()
        err = loop._validate_and_record_ack(ctx, "a1", "c1",
                                            {"nope": True})
        self.assertIsNotNone(err)


class TestFrameIdentity(unittest.TestCase):
    def test_frame_chain_and_parent_stored(self):
        sm = SessionManager()
        f = sm.register_frame("t1", "sub:aa", "win_default", None,
                              url="https://example.com/frame",
                              parent_frame_id="main",
                              frame_chain=["main", "sub:aa"],
                              name="child", chromium_frame_id=7)
        self.assertEqual(f.parent_frame_id, "main")
        self.assertEqual(f.frame_chain, ["main", "sub:aa"])
        self.assertEqual(f.name, "child")
        self.assertEqual(f.chromium_frame_id, 7)
        d = f.to_dict()
        self.assertEqual(d["frame_chain"], ["main", "sub:aa"])

    def test_document_preserved_across_snapshots(self):
        sm = SessionManager()
        f1 = sm.register_frame("t1", "main", url="https://example.com")
        doc1 = f1.document_id
        f2 = sm.register_frame("t1", "main", url="https://example.com")
        self.assertEqual(f2.document_id, doc1)

    def test_navigation_replaces_document_identity(self):
        sm = SessionManager()
        f1 = sm.register_frame("t1", "main", url="https://a.com")
        doc1 = f1.document_id
        sm.navigate("t1", "https://b.com")
        tab = sm.tab_identity("t1")
        self.assertIsNotNone(tab)
        f2 = sm.register_frame("t1", "main", url="https://b.com")
        self.assertNotEqual(f2.document_id, doc1)
        self.assertEqual(f2.url, "https://b.com")

    def test_subframe_document_replaced_on_navigation(self):
        sm = SessionManager()
        sub = sm.register_frame("t1", "sub:aa", url="https://a.com/f",
                                parent_frame_id="main",
                                frame_chain=["main", "sub:aa"])
        doc1 = sub.document_id
        sm.navigate("t1", "https://b.com")
        sub2 = sm.register_frame("t1", "sub:aa", url="https://b.com/f")
        self.assertNotEqual(sub2.document_id, doc1)

    def test_stable_id_never_uses_hash(self):
        a = stable_id("doc", "main", "https://example.com")
        b = stable_id("doc", "main", "https://example.com")
        self.assertEqual(a, b)
        self.assertTrue(a.startswith("doc_"))


class TestExtensionSyntax(unittest.TestCase):
    REPO = Path(__file__).resolve().parent.parent

    def _node_check(self, rel):
        node = shutil.which("node")
        if not node:
            self.skipTest("node not installed")
        p = self.REPO / rel
        r = subprocess.run([node, "--check", str(p)],
                           capture_output=True, text=True, timeout=30)
        self.assertEqual(r.returncode, 0,
                         f"{rel} failed node --check: {r.stderr}")

    def test_content_js_syntax(self):
        self._node_check("extension/content.js")

    def test_background_js_syntax(self):
        self._node_check("extension/background.js")

    def test_content_js_has_honest_ack(self):
        src = (self.REPO / "extension/content.js").read_text()
        self.assertIn("BROWSER_ACK", src)
        self.assertIn("executed = true", src)
        self.assertIn("__synk", src)

    def test_background_js_never_claims_execution_without_ack(self):
        src = (self.REPO / "extension/background.js").read_text()
        self.assertIn("executed: raw.executed === true", src)
        self.assertIn("FRAME_GONE", src)


class TestOwnedRuntimeSurface(unittest.TestCase):
    def test_default_profile_dir_is_synk_owned(self):
        self.assertIn(".synk", DEFAULT_PROFILE_DIR)
        self.assertNotIn("Chrome", DEFAULT_PROFILE_DIR)

    def test_describe_dispatch_builds_exact_wire_payload(self):
        sessions = SessionManager()
        ctx = ContextManager()
        rt = ExtensionBrowserRuntime(sessions, ctx)
        rt.connect()
        tgt = make_target(frame_chain=["main", "sub:aa"],
                          shadow_path=["#host"])
        msg = rt.describe_dispatch("click", tgt, {}, action_id="a1")
        self.assertEqual(msg["type"], "EXECUTE")
        self.assertEqual(msg["target"]["frame_chain"], ["main", "sub:aa"])
        self.assertEqual(msg["target"]["shadow_path"], ["#host"])
        self.assertEqual(msg["target"]["locator"]["value"], "#go")

    def test_describe_dispatch_requires_connection(self):
        sessions = SessionManager()
        ctx = ContextManager()
        rt = ExtensionBrowserRuntime(sessions, ctx)
        with self.assertRaises(Exception):
            rt.describe_dispatch("click", make_target(), {})

    def test_owned_wait_for_rejects_unknown_condition_shape(self):
        # wait_for documents its condition grammar; the check below is
        # static because no real browser exists here.
        import inspect
        from harness import browser_owned
        src = inspect.getsource(browser_owned.OwnedBrowserRuntime.wait_for)
        self.assertIn("unknown wait condition", src)


class TestMultiTabPrompt(unittest.TestCase):
    def test_prompt_for_tab_pins_to_task_tab(self):
        """The planner must plan against the task's pinned tab, not the
        first tab ever ingested (regression: ingest() only refreshed
        `current` for tab 'default')."""
        from harness.context_manager import ContextManager
        cm = ContextManager()
        cm.ingest("https://a.example/", [{"role": "button", "name": "A",
                    "tag": "button", "selector": "#a", "interactive": True}],
                  tab_id="tabA")
        cm.ingest("https://b.example/", [{"role": "textbox", "name": "B",
                    "tag": "input", "selector": "#b", "interactive": True}],
                  tab_id="tabB")
        pa = cm.prompt_for_tab("fill", "tabA")
        pb = cm.prompt_for_tab("fill", "tabB")
        self.assertIn("tabA", pa)
        self.assertIn("tabB", pb)
        self.assertIn("button 'A'", pa)
        self.assertIn("textbox 'B'", pb)
        self.assertNotIn("textbox 'B'", pa)
        self.assertNotIn("button 'A'", pb)

    def test_prompt_for_tab_falls_back_when_tab_unknown(self):
        from harness.context_manager import ContextManager
        cm = ContextManager()
        cm.ingest("https://a.example/", [{"role": "button", "name": "A",
                    "tag": "button", "selector": "#a", "interactive": True}],
                  tab_id="tabA")
        p = cm.prompt_for_tab("fill", "never-seen-tab")
        self.assertIn("Goal: fill", p)

    def test_agent_loop_plans_against_pinned_tab(self):
        """End-to-end through the AgentLoop: two tabs ingested, the task
        pinned to the second tab must get an act decision against the
        second tab's elements."""
        from harness.server import State
        s = State(":memory:")
        s.ctx.ingest("https://a.example/",
                     [{"role": "button", "name": "Go", "tag": "button",
                       "selector": "#go", "interactive": True}], tab_id="tabA")
        s.ctx.ingest("https://b.example/",
                     [{"role": "textbox", "name": "Name", "tag": "input",
                       "selector": "#name", "interactive": True}],
                     tab_id="tabB", frame_id="main")
        ctx = s.agent.begin("fill the name",
                            identity={"tab_id": "tabB",
                                      "window_id": "win_default",
                                      "frame_id": "main"})
        flags = s.agent.observe(ctx, {"url": "https://b.example/"})
        n = s.agent.next_action(ctx, flags)
        self.assertEqual(n["decision"], "act")
        self.assertEqual(n["action"]["tool"], "type")
        self.assertEqual(n["action"]["selector"], "#name")


if __name__ == "__main__":
    unittest.main()
