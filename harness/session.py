"""Canonical runtime identity for SYNK (Stage B / mandate Phase 1).

Every task, action, event, and evidence record in the runtime carries explicit
identifiers from this vocabulary:

    runtime_id, session_id, window_id, tab_id, frame_id, task_id,
    transaction_id, action_id, observation_id, lease_id, claim_id,
    evidence_id, capability_id, trace_id

The browser execution target is ALWAYS captured when a task/action is created
(session -> window -> tab -> frame). The runtime must never infer the target
from the browser's "current tab" while a task is running; that race is what
this module exists to eliminate.

Identifiers that must be stable across processes (observation ids, element
keys) are derived from SHA-256 over canonical JSON, never Python hash().
"""
from __future__ import annotations

import hashlib
import json
import threading
import time
import uuid

# The full identifier vocabulary. Not every record carries every id, but any
# id that appears on a record must come from this set and be minted here.
IDENTIFIERS = (
    "runtime_id", "session_id", "window_id", "tab_id", "frame_id",
    "task_id", "transaction_id", "action_id", "observation_id",
    "lease_id", "claim_id", "evidence_id", "capability_id", "trace_id",
)

MAIN_FRAME = "main"


def new_id(prefix: str) -> str:
    """Mint a fresh unique identifier with a readable prefix."""
    return f"{prefix}_{uuid.uuid4().hex[:12]}"


def stable_id(prefix: str, *parts: str) -> str:
    """Deterministic identifier from canonical content (sha256, not hash())."""
    canonical = json.dumps(list(parts), sort_keys=True, separators=(",", ":"))
    digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:16]
    return f"{prefix}_{digest}"


def canonical_origin(url: str) -> str:
    """scheme://host[:port] - the security identity, not just a hostname."""
    try:
        scheme, rest = url.split("://", 1)
        hostport = rest.split("/", 1)[0].lower()
        return f"{scheme.lower()}://{hostport}"
    except (ValueError, IndexError, AttributeError):
        return ""


class Frame:
    """A single document frame inside a tab.

    Stage D: frames carry their position in the frame tree
    (parent_frame_id + frame_chain from the main frame) and, when the
    runtime knows it, the browser's own frame id (Chrome's numeric
    sender.frameId / CDP frameId). Document identity is stable while the
    frame's document lives; navigation (or a frame tree rebuild) replaces
    it. Element refs pinned against a document_id fail closed once the
    document is replaced.
    """

    def __init__(self, frame_id: str, url: str = "",
                 parent_frame_id: str | None = None,
                 frame_chain: list | None = None,
                 name: str = "",
                 chromium_frame_id: int | None = None):
        self.frame_id = frame_id
        self.url = url
        self.origin = canonical_origin(url)
        self.parent_frame_id = parent_frame_id
        if frame_chain is not None:
            self.frame_chain = list(frame_chain)
        elif parent_frame_id is None:
            self.frame_chain = [frame_id]
        else:
            self.frame_chain = [MAIN_FRAME, frame_id]
        self.name = name
        self.chromium_frame_id = chromium_frame_id
        self.document_id = stable_id("doc", frame_id, self.origin)
        self.observation_version = 0      # monotonic int, per frame
        self.observation_id: str | None = None
        self.snapshot_hash: str | None = None

    def replace_document(self, url: str = "") -> None:
        """Navigation / frame rebuild: new document identity, fresh version."""
        if url:
            self.url = url
            self.origin = canonical_origin(url)
        self.document_id = stable_id("doc", self.frame_id, self.origin,
                                     str(time.time_ns()))
        self.observation_version = 0
        self.observation_id = None
        self.snapshot_hash = None

    def to_dict(self) -> dict:
        return {"frame_id": self.frame_id, "url": self.url,
                "origin": self.origin, "document_id": self.document_id,
                "parent_frame_id": self.parent_frame_id,
                "frame_chain": self.frame_chain,
                "name": self.name,
                "chromium_frame_id": self.chromium_frame_id,
                "observation_version": self.observation_version,
                "observation_id": self.observation_id,
                "snapshot_hash": self.snapshot_hash}


class Tab:
    """A browser tab: the unit of execution targeting."""

    def __init__(self, tab_id: str, window_id: str, url: str = "", title: str = ""):
        self.tab_id = tab_id
        self.window_id = window_id
        self.url = url
        self.title = title
        self.origin = canonical_origin(url)
        self.active_frame_id = MAIN_FRAME
        self.frames: dict[str, Frame] = {MAIN_FRAME: Frame(MAIN_FRAME, url)}
        self.opened_at = time.time()
        self.last_observation_id: str | None = None

    def frame(self, frame_id: str = MAIN_FRAME,
              parent_frame_id: str | None = None,
              frame_chain: list | None = None,
              name: str = "",
              chromium_frame_id: int | None = None) -> Frame:
        f = self.frames.get(frame_id)
        if f is None:
            f = Frame(frame_id, self.url,
                      parent_frame_id=parent_frame_id,
                      frame_chain=frame_chain, name=name,
                      chromium_frame_id=chromium_frame_id)
            self.frames[frame_id] = f
        else:
            # Preserve document identity across snapshot events: an existing
            # frame keeps its document_id. Only explicit metadata updates.
            if name:
                f.name = name
            if chromium_frame_id is not None:
                f.chromium_frame_id = chromium_frame_id
            if parent_frame_id is not None:
                f.parent_frame_id = parent_frame_id
            if frame_chain is not None:
                f.frame_chain = list(frame_chain)
        return f

    def navigate(self, url: str, title: str = "") -> None:
        """Navigation replaces the main-frame document (new document
        identity) and rebuilds the frame tree: subframe documents are
        replaced too, since their documents die with the navigation."""
        self.url = url
        self.title = title or self.title
        self.origin = canonical_origin(url)
        self.frames[MAIN_FRAME].replace_document(url)
        for fid, f in self.frames.items():
            if fid != MAIN_FRAME:
                f.replace_document()

    def identity(self, session_id: str) -> dict:
        return {"session_id": session_id, "window_id": self.window_id,
                "tab_id": self.tab_id, "frame_id": self.active_frame_id}

    def to_dict(self, session_id: str) -> dict:
        return {"tab_id": self.tab_id, "window_id": self.window_id,
                "session_id": session_id, "url": self.url, "title": self.title,
                "origin": self.origin, "active_frame_id": self.active_frame_id,
                "frames": {fid: f.to_dict() for fid, f in self.frames.items()},
                "last_observation_id": self.last_observation_id}


class Window:
    def __init__(self, window_id: str):
        self.window_id = window_id
        self.tabs: dict[str, Tab] = {}
        self.active_tab_id: str | None = None

    def to_dict(self, session_id: str) -> dict:
        return {"window_id": self.window_id, "active_tab_id": self.active_tab_id,
                "tabs": {tid: t.to_dict(session_id) for tid, t in self.tabs.items()}}


class Session:
    """One browser session: windows -> tabs -> frames -> documents."""

    def __init__(self, session_id: str, runtime_id: str):
        self.session_id = session_id
        self.runtime_id = runtime_id
        self.windows: dict[str, Window] = {}
        self.created_at = time.time()

    def to_dict(self) -> dict:
        return {"session_id": self.session_id, "runtime_id": self.runtime_id,
                "windows": {wid: w.to_dict(self.session_id)
                            for wid, w in self.windows.items()}}


class SessionManager:
    """Thread-safe registry of the canonical session tree.

    Extension push mode and CDP pull mode both register through here, so
    every event and action resolves to the same (session, window, tab, frame)
    identity model.
    """

    def __init__(self, runtime_id: str | None = None):
        self.runtime_id = runtime_id or new_id("rt")
        self._lock = threading.RLock()
        self.sessions: dict[str, Session] = {}
        self.default_session_id: str | None = None

    # -- sessions ----------------------------------------------------------
    def get_or_create_session(self, session_id: str | None = None) -> Session:
        with self._lock:
            if session_id:
                if session_id in self.sessions:
                    return self.sessions[session_id]
            elif (self.default_session_id is not None
                    and self.default_session_id in self.sessions):
                return self.sessions[self.default_session_id]
            sid = session_id or new_id("sess")
            sess = Session(sid, self.runtime_id)
            self.sessions[sid] = sess
            if self.default_session_id is None:
                self.default_session_id = sid
            return sess

    # -- registration ------------------------------------------------------
    def register_tab(self, tab_id: str, window_id: str = "win_default",
                     session_id: str | None = None,
                     url: str = "", title: str = "") -> Tab:
        """Idempotent: registering an existing tab updates its URL/title."""
        with self._lock:
            sess = self.get_or_create_session(session_id)
            win = sess.windows.get(window_id)
            if win is None:
                win = Window(window_id)
                sess.windows[window_id] = win
            tab = win.tabs.get(tab_id)
            if tab is None:
                tab = Tab(tab_id, window_id, url, title)
                win.tabs[tab_id] = tab
                if win.active_tab_id is None:
                    win.active_tab_id = tab_id
            else:
                if url:
                    tab.url = url
                if title:
                    tab.title = title
                tab.origin = canonical_origin(tab.url)
            return tab

    def register_frame(self, tab_id: str, frame_id: str,
                       window_id: str = "win_default",
                       session_id: str | None = None, url: str = "",
                       parent_frame_id: str | None = None,
                       frame_chain: list | None = None,
                       name: str = "",
                       chromium_frame_id: int | None = None) -> Frame:
        with self._lock:
            tab = self.register_tab(tab_id, window_id, session_id, url=url)
            frame = tab.frame(frame_id, parent_frame_id=parent_frame_id,
                              frame_chain=frame_chain, name=name,
                              chromium_frame_id=chromium_frame_id)
            if url:
                # Snapshot from the same document: URL updates, identity
                # stays. (A real navigation goes through tab.navigate().)
                frame.url = url
                frame.origin = canonical_origin(url)
            return frame

    def record_observation(self, tab_id: str, frame_id: str = MAIN_FRAME,
                           snapshot_hash: str | None = None,
                           window_id: str = "win_default",
                           session_id: str | None = None) -> dict:
        """Mint a new immutable observation version for a frame.

        Returns the observation identity; versions are monotonic ints and the
        observation_id is a sha256 over (frame, version, snapshot hash).
        """
        with self._lock:
            frame = self.register_frame(tab_id, frame_id, window_id, session_id)
            frame.observation_version += 1
            frame.snapshot_hash = snapshot_hash
            frame.observation_id = stable_id(
                "obs", frame.document_id, str(frame.observation_version),
                snapshot_hash or "")
            tab = self.register_tab(tab_id, window_id, session_id)
            tab.last_observation_id = frame.observation_id
            sess = self.get_or_create_session(session_id)
            return {"session_id": sess.session_id, "window_id": window_id,
                    "tab_id": tab_id, "frame_id": frame_id,
                    "observation_id": frame.observation_id,
                    "observation_version": frame.observation_version,
                    "document_id": frame.document_id,
                    "snapshot_hash": snapshot_hash}

    def navigate(self, tab_id: str, url: str, title: str = "",
                 window_id: str = "win_default",
                 session_id: str | None = None) -> Tab:
        with self._lock:
            tab = self.register_tab(tab_id, window_id, session_id)
            tab.navigate(url, title)
            return tab

    # -- reads -------------------------------------------------------------
    def tab_url(self, tab_id: str,
                session_id: str | None = None) -> str | None:
        """Current URL of a tab, or None if not registered."""
        with self._lock:
            for sid, sess in self.sessions.items():
                if session_id and sid != session_id:
                    continue
                for win in sess.windows.values():
                    tab = win.tabs.get(tab_id)
                    if tab is not None:
                        return tab.url
            return None

    def tab_identity(self, tab_id: str,
                     session_id: str | None = None) -> dict | None:
        """Resolve a tab_id to its full canonical identity, or None."""
        with self._lock:
            for sid, sess in self.sessions.items():
                if session_id and sid != session_id:
                    continue
                for wid, win in sess.windows.items():
                    if tab_id in win.tabs:
                        return win.tabs[tab_id].identity(sid)
            return None

    def frame_document_id(self, tab_id: str, frame_id: str = "main",
                          session_id: str | None = None) -> str | None:
        """Current document identity for a frame, or None if unknown.

        Stage E: WebMCP handle scoping reads this at invoke time, so a
        navigation (which replaces the document_id) is always detected.
        """
        with self._lock:
            for sid, sess in self.sessions.items():
                if session_id and sid != session_id:
                    continue
                for win in sess.windows.values():
                    tab = win.tabs.get(tab_id)
                    if tab is not None:
                        frame = tab.frames.get(frame_id or "main")
                        return frame.document_id if frame else None
            return None

    def frame_known(self, tab_id: str, frame_id: str = "main",
                    session_id: str | None = None) -> bool:
        """True when the frame is registered on the tab.

        Stage E: scope_for_tab fails closed on unknown frames -- a
        handle must never be minted for a frame the session never saw.
        """
        with self._lock:
            for sid, sess in self.sessions.items():
                if session_id and sid != session_id:
                    continue
                for win in sess.windows.values():
                    tab = win.tabs.get(tab_id)
                    if tab is not None:
                        return (frame_id or "main") in tab.frames
            return False

    def snapshot(self) -> dict:
        with self._lock:
            return {"runtime_id": self.runtime_id,
                    "sessions": {sid: s.to_dict() for sid, s in self.sessions.items()}}
