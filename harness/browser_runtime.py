"""BrowserRuntime: the ONE browser adapter interface (Stage D / mandate Phase 6).

Every browser execution path in SYNK -- the attached extension, the
SYNK-owned Playwright/CDP browser, benchmarks, and tests -- implements this
interface. The mandate's core rule is structural: the adapter NEVER guesses
the execution target from the browser's "current tab". Every method takes
explicit (session_id, window_id, tab_id, frame_id) identity; a missing tab
id is a typed error, not a fallback to "default".

Two honest modes implement this interface:

* ``ExtensionBrowserRuntime`` (harness/browser_extension.py): the user's
  EXISTING browser, driven through the extension. Read methods are backed
  by real pushed snapshots; mutations are dispatched through the
  closed-loop agent (/agent/*) because the harness cannot synchronously
  drive someone else's browser tab.

* ``OwnedBrowserRuntime`` (harness/browser_owned.py): a SYNK-managed
  Chromium (Playwright/CDP) with a persistent profile, or an explicit
  attach to a debugging endpoint the operator controls. This is NOT the
  user's browser and never claims to be.

Stdlib only. Playwright stays behind a lazy import in the owned adapter.
No Python hash() for identity.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Callable

from .session import MAIN_FRAME, new_id, stable_id


# -- typed errors ---------------------------------------------------------------
class BrowserRuntimeError(Exception):
    """Base for all browser-adapter failures."""


class BrowserNotConnected(BrowserRuntimeError):
    """connect() was never called or the connection dropped."""


class TabGone(BrowserRuntimeError):
    """The pinned tab no longer exists (closed / crashed / navigated away)."""


class FrameGone(BrowserRuntimeError):
    """The pinned frame no longer exists in its tab."""


class ElementNotFound(BrowserRuntimeError):
    """A resolved locator matched zero elements."""


class AmbiguousElement(BrowserRuntimeError):
    """A resolved locator matched more than one element (fail closed)."""


class UnsupportedOperation(BrowserRuntimeError):
    """The adapter cannot perform this operation by design.

    Used where the architecture forbids an action -- e.g. the harness
    cannot synchronously mutate the user's attached browser tab; those
    mutations travel through the closed-loop agent instead. Raising is
    honest; silently pretending is not.
    """


class StaleTarget(BrowserRuntimeError):
    """The target's document changed since the action was planned."""


# -- health ----------------------------------------------------------------------
HEALTHY = "HEALTHY"
DEGRADED = "DEGRADED"
UNRESPONSIVE = "UNRESPONSIVE"
CRASHED = "CRASHED"
CLOSED = "CLOSED"

TAB_HEALTH_STATES = (HEALTHY, DEGRADED, UNRESPONSIVE, CRASHED, CLOSED)


@dataclass
class TabHealth:
    tab_id: str
    state: str = HEALTHY
    last_event_ts: float = field(default_factory=time.time)
    consecutive_failures: int = 0
    note: str = ""

    def record_success(self) -> None:
        self.consecutive_failures = 0
        self.last_event_ts = time.time()
        if self.state in (DEGRADED, UNRESPONSIVE):
            self.state = HEALTHY
            self.note = "recovered"

    def record_failure(self, note: str = "") -> None:
        self.consecutive_failures += 1
        self.last_event_ts = time.time()
        self.note = note
        if self.consecutive_failures >= 5:
            self.state = UNRESPONSIVE
        elif self.consecutive_failures >= 2:
            self.state = DEGRADED

    def mark_crashed(self, note: str = "") -> None:
        self.state = CRASHED
        self.note = note
        self.last_event_ts = time.time()

    def mark_closed(self) -> None:
        self.state = CLOSED
        self.last_event_ts = time.time()


# -- observations and acknowledgements --------------------------------------------
@dataclass
class BrowserObservation:
    """Post-state read from the browser AFTER an interaction (or a plain
    observe()). This is the evidence the verifier judges claims against --
    never the executor's word that it "ran"."""
    observation_id: str
    session_id: str | None
    window_id: str
    tab_id: str
    frame_id: str
    url: str
    title: str = ""
    # Element-level post-state keyed by the target that was acted on.
    target_state: dict = field(default_factory=dict)
    # Full node list when the adapter produced one (snapshot shape).
    nodes: list = field(default_factory=list)
    captured_at: float = field(default_factory=time.time)

    def to_dict(self) -> dict:
        return {"observation_id": self.observation_id,
                "session_id": self.session_id, "window_id": self.window_id,
                "tab_id": self.tab_id, "frame_id": self.frame_id,
                "url": self.url, "title": self.title,
                "target_state": self.target_state,
                "node_count": len(self.nodes),
                "captured_at": self.captured_at}


@dataclass
class BrowserAck:
    """Honest execution acknowledgement.

    accepted: the adapter accepted/validated the command.
    executed: the browser actually performed the interaction.
    observed: BrowserObservation with the post-state (present exactly when
              executed is True; its absence means "not yet observed").
    The old lie -- ok=True before the browser ran anything -- is
    unrepresentable here: executed=True requires an observation.
    """
    ack_id: str
    action_id: str | None
    session_id: str | None
    window_id: str
    tab_id: str
    frame_id: str
    command: str
    accepted: bool
    executed: bool
    observed: BrowserObservation | None = None
    error: str | None = None
    error_code: str | None = None

    def __post_init__(self):
        if self.executed and self.observed is None:
            raise ValueError("executed=True requires an observation")

    def to_dict(self) -> dict:
        return {"ack_id": self.ack_id, "action_id": self.action_id,
                "session_id": self.session_id, "window_id": self.window_id,
                "tab_id": self.tab_id, "frame_id": self.frame_id,
                "command": self.command, "accepted": self.accepted,
                "executed": self.executed,
                "observed": self.observed.to_dict()
                if self.observed else None,
                "error": self.error, "error_code": self.error_code}


@dataclass
class ElementTarget:
    """A fully-resolved execution target: no opaque refs past this point.

    frame_chain: ordered frame ids from the tab root to the target frame
                 (["main"] for the top document).
    shadow_path: ordered shadow-host selectors from the frame document to
                 the target's shadow tree (empty for light DOM).
    locator: {"strategy": ..., "value": ...} usable by the adapter.
    """
    session_id: str | None
    window_id: str
    tab_id: str
    frame_id: str
    frame_chain: list = field(default_factory=lambda: [MAIN_FRAME])
    shadow_path: list = field(default_factory=list)
    locator: dict = field(default_factory=dict)
    ref_id: int | None = None
    snapshot_version: int | None = None

    def identity(self) -> dict:
        return {"session_id": self.session_id, "window_id": self.window_id,
                "tab_id": self.tab_id, "frame_id": self.frame_id,
                "frame_chain": list(self.frame_chain),
                "shadow_path": list(self.shadow_path),
                "locator": dict(self.locator)}


def observation_id_for(tab_id: str, frame_id: str, url: str,
                       nonce: str = "") -> str:
    """Stable, content-derived observation id (sha256, never hash())."""
    return stable_id("obs", tab_id, frame_id, url, nonce or new_id("n"))


class BrowserRuntime:
    """Abstract browser adapter. All ids explicit; no "current tab" reads.

    Implementations must raise the typed errors above instead of guessing.
    Methods that cannot be implemented honestly by a mode raise
    UnsupportedOperation with a message naming the supported path.
    """

    mode: str = "abstract"  # "attached-extension" | "owned-launch" | ...

    # -- lifecycle ----------------------------------------------------------
    def connect(self) -> dict:
        """Establish the browser session. Returns {"mode", "session_id"}."""
        raise NotImplementedError

    def disconnect(self) -> None:
        raise NotImplementedError

    @property
    def connected(self) -> bool:
        raise NotImplementedError

    # -- tabs ---------------------------------------------------------------
    def list_tabs(self, *, window_id: str | None = None,
                  session_id: str | None = None) -> list[dict]:
        """Canonical tab list: [{tab_id, window_id, url, title, health}]."""
        raise NotImplementedError

    def tab_health(self, tab_id: str) -> TabHealth:
        raise NotImplementedError

    # -- observation --------------------------------------------------------
    def observe(self, *, tab_id: str, frame_id: str = MAIN_FRAME,
                session_id: str | None = None,
                window_id: str = "win_default") -> BrowserObservation:
        """Read the current state of the pinned tab/frame. Never mutates."""
        raise NotImplementedError

    def locate(self, target: ElementTarget) -> dict:
        """Resolve target to adapter handles WITHOUT acting.

        Returns {"found": bool, "count": int, "target_state": dict}.
        Raises ElementNotFound / AmbiguousElement (fail closed).
        """
        raise NotImplementedError

    # -- interaction primitives ---------------------------------------------
    # Each returns a BrowserAck with executed=True + observation, or raises
    # a typed error. "Command accepted" without browser execution is
    # represented as accepted=True, executed=False -- never as success.
    def click(self, target: ElementTarget, *,
              action_id: str | None = None) -> BrowserAck:
        raise NotImplementedError

    def type(self, target: ElementTarget, text: str, *,
             action_id: str | None = None,
             clear_first: bool = True) -> BrowserAck:
        raise NotImplementedError

    def select(self, target: ElementTarget, value: str, *,
               action_id: str | None = None) -> BrowserAck:
        raise NotImplementedError

    def scroll(self, *, tab_id: str, frame_id: str = MAIN_FRAME,
               direction: str = "down", amount: int = 600,
               session_id: str | None = None,
               window_id: str = "win_default",
               action_id: str | None = None) -> BrowserAck:
        raise NotImplementedError

    def keypress(self, target: ElementTarget | None, key: str, *,
                 action_id: str | None = None,
                 tab_id: str | None = None,
                 frame_id: str = MAIN_FRAME,
                 session_id: str | None = None,
                 window_id: str = "win_default") -> BrowserAck:
        raise NotImplementedError

    def navigate(self, url: str, *, tab_id: str,
                 frame_id: str = MAIN_FRAME,
                 session_id: str | None = None,
                 window_id: str = "win_default",
                 action_id: str | None = None,
                 timeout_ms: int = 30000) -> BrowserAck:
        raise NotImplementedError

    def wait_for(self, *, tab_id: str, frame_id: str = MAIN_FRAME,
                 condition: str, timeout_ms: int = 10000,
                 session_id: str | None = None,
                 window_id: str = "win_default") -> BrowserObservation:
        """Block until a condition holds ("load", "idle", "selector:<css>",
        "url:<prefix>") or raise TimeoutError."""
        raise NotImplementedError

    # -- inspection ---------------------------------------------------------
    def screenshot(self, *, tab_id: str, frame_id: str = MAIN_FRAME,
                   session_id: str | None = None,
                   window_id: str = "win_default") -> bytes:
        raise NotImplementedError

    def inspect_network(self, *, tab_id: str,
                        session_id: str | None = None,
                        since_ts: float = 0.0) -> list[dict]:
        """Network requests observed for the tab since since_ts."""
        raise NotImplementedError

    def inspect_dialogs(self, *, tab_id: str,
                        session_id: str | None = None) -> list[dict]:
        """Open dialogs (alert/confirm/prompt/beforeunload/<dialog>)."""
        raise NotImplementedError

    def inspect_downloads(self, *, tab_id: str,
                          session_id: str | None = None) -> list[dict]:
        """Download events for the tab."""
        raise NotImplementedError

    # -- events -------------------------------------------------------------
    def on_event(self, callback: Callable[[str, dict], Any]) -> None:
        """Subscribe to lifecycle events: tab.opened/closed, navigation,
        popup, dialog.opened/closed, download, crash, network.*."""
        raise NotImplementedError
