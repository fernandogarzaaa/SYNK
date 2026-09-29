"""Adapter A: the user's ATTACHED browser via the extension (Stage D).

This is the user's existing browser -- their profile, cookies, logins,
tabs, and extensions. The harness never launches it, never attaches a
debugger to it, and never guesses its "current tab": every read and every
command carries explicit (session, window, tab, frame) identity, pushed
by the extension's content scripts (which learn their tab id from the
background page) and pinned by the closed-loop agent at task start.

Architectural boundary (honest, not a stub): the harness cannot
synchronously drive a tab in someone else's browser. Reads
(list_tabs/observe/locate) are served from real pushed snapshots via the
SessionManager + ContextManager. Mutations raise UnsupportedOperation
naming the supported path -- the Stage C closed loop (/agent/begin ->
/agent/next -> extension EXECUTE -> BROWSER_ACK -> /snapshot ->
/agent/report) -- through which every extension action already flows
with the honest lifecycle DISPATCHED -> browser ACK -> OBSERVED ->
VERIFIED. ``describe_dispatch`` builds the exact EXECUTE wire payload
for that loop.

Stdlib only.
"""
from __future__ import annotations

import time
from typing import Any, Callable

from .browser_runtime import (BrowserAck, BrowserObservation, BrowserRuntime,
                              ElementNotFound, AmbiguousElement, ElementTarget,
                              TabGone, TabHealth, UnsupportedOperation,
                              observation_id_for)
from .interactions import build_execute_message
from .session import MAIN_FRAME

_CLOSED_LOOP = ("mutation not supported by direct dispatch: the attached "
                "browser is the user's own tab; execute through the "
                "closed-loop agent instead (/agent/begin -> /agent/next -> "
                "extension EXECUTE -> BROWSER_ACK -> /snapshot -> "
                "/agent/report), which runs the honest transaction "
                "lifecycle via the ExecutionGateway")


class ExtensionBrowserRuntime(BrowserRuntime):
    """BrowserRuntime over the attached extension (read side is real)."""

    mode = "attached-extension"

    def __init__(self, sessions, ctx):
        # sessions: SessionManager, ctx: ContextManager. Kept duck-typed
        # so tests can substitute fakes.
        self._sessions = sessions
        self._ctx = ctx
        self._connected = False
        self._session_id: str | None = None
        self._health: dict[str, TabHealth] = {}
        self._callbacks: list[Callable[[str, dict], Any]] = []

    # -- lifecycle ----------------------------------------------------------
    def connect(self) -> dict:
        # Logical connection: the extension pushes to the harness; there
        # is no debugger to attach and no browser to launch. connect()
        # marks the adapter ready to serve reads from pushed state.
        self._connected = True
        sess = self._sessions.get_or_create_session()
        self._session_id = sess.session_id
        return {"mode": self.mode, "session_id": self._session_id,
                "note": "attached to the user's browser via the extension; "
                        "no browser was launched or debugged"}

    def disconnect(self) -> None:
        self._connected = False

    @property
    def connected(self) -> bool:
        return self._connected

    def _require(self) -> None:
        if not self._connected:
            from .browser_runtime import BrowserNotConnected
            raise BrowserNotConnected(
                "extension adapter not connected; call connect() first")

    def _health_for(self, tab_id: str) -> TabHealth:
        h = self._health.get(tab_id)
        if h is None:
            h = TabHealth(tab_id=tab_id)
            self._health[tab_id] = h
        return h

    # -- tabs ---------------------------------------------------------------
    def list_tabs(self, *, window_id: str | None = None,
                  session_id: str | None = None) -> list[dict]:
        self._require()
        snap = self._sessions.snapshot()
        out = []
        for sid, sess in snap["sessions"].items():
            if session_id and sid != session_id:
                continue
            for wid, win in sess["windows"].items():
                if window_id and wid != window_id:
                    continue
                for tid, tab in win["tabs"].items():
                    out.append({"tab_id": tid, "window_id": wid,
                                "session_id": sid, "url": tab["url"],
                                "title": tab["title"],
                                "health": self._health_for(tid).state})
        return out

    def tab_health(self, tab_id: str) -> TabHealth:
        self._require()
        tabs = [t for t in self.list_tabs() if t["tab_id"] == tab_id]
        if not tabs:
            raise TabGone(f"tab {tab_id} not registered by the extension")
        return self._health_for(tab_id)

    # -- observation --------------------------------------------------------
    def _snapshot_nodes(self, tab_id: str) -> tuple[dict, list[dict]]:
        snap = self._ctx._tab_snapshots.get(tab_id)
        if snap is None:
            raise TabGone(
                f"tab {tab_id} has no ingested snapshot; the extension "
                f"has not pushed one for this tab")
        return snap, snap.get("nodes", [])

    def observe(self, *, tab_id: str, frame_id: str = MAIN_FRAME,
                session_id: str | None = None,
                window_id: str = "win_default") -> BrowserObservation:
        self._require()
        snap, nodes = self._snapshot_nodes(tab_id)
        frame_nodes = [n for n in nodes
                       if n.get("frame_id", MAIN_FRAME) == frame_id]
        url = snap.get("url", "")
        self._health_for(tab_id).record_success()
        return BrowserObservation(
            observation_id=observation_id_for(tab_id, frame_id, url,
                                              snap.get("hash", "")),
            session_id=session_id, window_id=window_id,
            tab_id=tab_id, frame_id=frame_id, url=url,
            title="", nodes=nodes,
            target_state={"frame_node_count": len(frame_nodes),
                          "snapshot_version": snap.get("version")})

    def locate(self, target: ElementTarget) -> dict:
        self._require()
        snap, nodes = self._snapshot_nodes(target.tab_id)
        locator = target.locator or {}
        value = locator.get("value", "")
        strategy = locator.get("strategy", "")
        matches = []
        for n in nodes:
            if target.frame_id != MAIN_FRAME and \
                    n.get("frame_id") != target.frame_id:
                continue
            nloc = n.get("locator") or {}
            # Match on the canonical locator the snapshot assigned.
            if strategy == "test-id":
                if (n.get("test_id") or n.get("data_testid")) == value:
                    matches.append(n)
            elif value and (n.get("selector") == value or
                            nloc.get("value") == value):
                matches.append(n)
        if not matches:
            raise ElementNotFound(
                f"locator {strategy}:{value} matched nothing in tab "
                f"{target.tab_id} (snapshot v{snap.get('version')})")
        if len(matches) > 1:
            raise AmbiguousElement(
                f"locator {strategy}:{value} matched {len(matches)} "
                f"elements in tab {target.tab_id}; failing closed")
        n = matches[0]
        return {"found": True, "count": 1,
                "target_state": {"selector": n.get("selector"),
                                 "role": n.get("role"),
                                 "name": n.get("name"),
                                 "value": n.get("value", ""),
                                 "checked": bool(n.get("checked", False)),
                                 "disabled": bool(n.get("disabled", False)),
                                 "visible": bool(n.get("visible", True)),
                                 "frame_id": n.get("frame_id", MAIN_FRAME)}}

    # -- mutations: the honest boundary --------------------------------------
    def describe_dispatch(self, command: str, target: ElementTarget,
                          args: dict | None = None,
                          action_id: str | None = None) -> dict:
        """Build the exact EXECUTE wire payload for the closed loop.

        The caller (the /agent/next handler or the extension's runTask)
        delivers this to the content script of the PINNED tab; the
        content script performs the robust primitive and replies with a
        BROWSER_ACK carrying the observed post-state.
        """
        self._require()
        return build_execute_message(command, target, args, action_id)

    def click(self, target: ElementTarget, *,
              action_id: str | None = None) -> BrowserAck:
        raise UnsupportedOperation(_CLOSED_LOOP)

    def type(self, target: ElementTarget, text: str, *,
             action_id: str | None = None,
             clear_first: bool = True) -> BrowserAck:
        raise UnsupportedOperation(_CLOSED_LOOP)

    def select(self, target: ElementTarget, value: str, *,
               action_id: str | None = None) -> BrowserAck:
        raise UnsupportedOperation(_CLOSED_LOOP)

    def scroll(self, *, tab_id: str, frame_id: str = MAIN_FRAME,
               direction: str = "down", amount: int = 600,
               session_id: str | None = None,
               window_id: str = "win_default",
               action_id: str | None = None) -> BrowserAck:
        raise UnsupportedOperation(_CLOSED_LOOP)

    def keypress(self, target: ElementTarget | None, key: str, *,
                 action_id: str | None = None,
                 tab_id: str | None = None,
                 frame_id: str = MAIN_FRAME,
                 session_id: str | None = None,
                 window_id: str = "win_default") -> BrowserAck:
        raise UnsupportedOperation(_CLOSED_LOOP)

    def navigate(self, url: str, *, tab_id: str,
                 frame_id: str = MAIN_FRAME,
                 session_id: str | None = None,
                 window_id: str = "win_default",
                 action_id: str | None = None,
                 timeout_ms: int = 30000) -> BrowserAck:
        raise UnsupportedOperation(_CLOSED_LOOP)

    def wait_for(self, *, tab_id: str, frame_id: str = MAIN_FRAME,
                 condition: str, timeout_ms: int = 10000,
                 session_id: str | None = None,
                 window_id: str = "win_default") -> BrowserObservation:
        raise UnsupportedOperation(
            "the attached browser is event-driven via pushed snapshots; "
            "wait on the next /snapshot for the pinned tab instead of "
            "blocking the harness")

    def screenshot(self, *, tab_id: str, frame_id: str = MAIN_FRAME,
                   session_id: str | None = None,
                   window_id: str = "win_default") -> bytes:
        raise UnsupportedOperation(
            "screenshots in attached mode travel through /vision/capture "
            "with image_base64 pushed by the extension; direct capture is "
            "not available to the harness")

    def inspect_network(self, *, tab_id: str,
                        session_id: str | None = None,
                        since_ts: float = 0.0) -> list[dict]:
        raise UnsupportedOperation(
            "network inspection requires the owned CDP adapter; the "
            "extension does not expose request telemetry")

    def inspect_dialogs(self, *, tab_id: str,
                        session_id: str | None = None) -> list[dict]:
        raise UnsupportedOperation(
            "dialog inspection requires the owned CDP adapter; in attached "
            "mode dialogs surface as dialog.opened bus events")

    def inspect_downloads(self, *, tab_id: str,
                          session_id: str | None = None) -> list[dict]:
        raise UnsupportedOperation(
            "download inspection requires the owned CDP adapter")

    # -- events -------------------------------------------------------------
    def on_event(self, callback: Callable[[str, dict], Any]) -> None:
        self._callbacks.append(callback)

    def note_tab_event(self, event: str, tab_id: str, note: str = "") -> None:
        """Feed lifecycle facts observed via pushed events (used by the
        server's event fan-out and by tests)."""
        h = self._health_for(tab_id)
        if event == "tab.closed":
            h.mark_closed()
        elif event == "tab.crashed":
            h.mark_crashed(note)
        elif event == "tab.recovered":
            h.state = "HEALTHY"
            h.consecutive_failures = 0
            h.note = note or "recovered"
        for cb in self._callbacks:
            try:
                cb(event, {"tab_id": tab_id, "note": note,
                           "ts": time.time()})
            except Exception:
                pass
