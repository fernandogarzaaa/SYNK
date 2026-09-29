"""WebMCP handle scoping (Stage E / mandate Phase 15).

A WebMCP tool handle is bound to exactly one (session, tab, frame,
document). A handle minted for tab A can never be invoked against tab B,
and a handle minted before a navigation fails closed afterwards because
the document identity changed.

Document identity comes from the SessionManager's Frame records: a
navigation replaces the document_id, so any stale handle is detected at
invoke time without trusting the caller.
"""
from __future__ import annotations

from dataclasses import dataclass

from ..session import MAIN_FRAME, stable_id


@dataclass(frozen=True)
class WebMCPScope:
    """The page context a WebMCP discovery/invocation is bound to."""
    session_id: str | None
    tab_id: str
    frame_id: str = MAIN_FRAME
    document_id: str | None = None

    def key(self) -> str:
        return "|".join((
            str(self.session_id or ""),
            str(self.tab_id),
            str(self.frame_id or MAIN_FRAME),
            str(self.document_id or ""),
        ))

    def handle_id_for(self, tool_name: str) -> str:
        """Deterministic handle id (sha256, never hash())."""
        return stable_id("webmcp-handle", self.key(), str(tool_name))

    def matches(self, other: "WebMCPScope") -> bool:
        return self.key() == other.key()

    def to_dict(self) -> dict:
        return {"session_id": self.session_id, "tab_id": self.tab_id,
                "frame_id": self.frame_id, "document_id": self.document_id}


def scope_for_tab(sessions, tab_id: str,
                  frame_id: str = MAIN_FRAME,
                  session_id: str | None = None) -> WebMCPScope | None:
    """Build the scope for a tab from live session state.

    Returns None when the tab is not registered (unknown tab -> the
    caller fails closed). The document_id is read from the session
    manager at call time, so navigation staleness is always current.
    """
    if sessions is None or not tab_id:
        return None
    try:
        ident = sessions.tab_identity(tab_id, session_id=session_id)
    except Exception:
        ident = None
    if ident is None:
        return None  # unknown tab -> the caller fails closed
    try:
        known = sessions.frame_known(tab_id, frame_id or MAIN_FRAME,
                                     session_id=session_id)
    except Exception:
        known = False
    if not known:
        return None  # unknown frame -> fail closed
    try:
        doc_id = sessions.frame_document_id(tab_id, frame_id,
                                            session_id=session_id)
    except Exception:
        doc_id = None
    # Store the CANONICAL session id resolved from session state, never
    # the caller's possibly-None hint: handles must always carry the
    # real session identity.
    return WebMCPScope(session_id=ident.get("session_id") or session_id,
                       tab_id=tab_id,
                       frame_id=frame_id or MAIN_FRAME,
                       document_id=doc_id)


def current_document_id(sessions, scope: WebMCPScope) -> str | None:
    """The live document_id for the scope's frame, or None if unknown."""
    if sessions is None or scope is None:
        return None
    try:
        return sessions.frame_document_id(scope.tab_id, scope.frame_id,
                                          session_id=scope.session_id)
    except Exception:
        return None
