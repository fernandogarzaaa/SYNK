"""RefResolver: agent ref -> adapter-executable ElementTarget (Stage D).

The mandate's rule: the browser adapter must never receive an opaque
agent ref integer directly. Every action carrying ``ref: 3`` is resolved
HERE, against the ContextManager's fail-closed ElementRefs, into an
``ElementTarget`` (session/tab/frame + frame_chain + shadow_path +
concrete locator). An unresolvable ref raises ``RefResolutionError``;
it is never stringified into a selector (the old ``page.click("3")``
bug class is unrepresentable by construction).

Stdlib only.
"""
from __future__ import annotations

from .browser_runtime import ElementTarget
from .session import MAIN_FRAME, canonical_origin


class RefResolutionError(Exception):
    """A ref could not be resolved to an executable target (fail closed)."""


def resolve_action_target(action: dict, ctx,
                          *, tab_id: str,
                          session_id: str | None = None,
                          window_id: str = "win_default",
                          frame_id: str = MAIN_FRAME,
                          page_url: str = "") -> ElementTarget:
    """Resolve an action's addressing to an ElementTarget.

    Addressing modes (in order):
      1. integer ``ref`` (+ optional ``ref_version``, ``frame_chain``,
         ``shadow_path``): resolved through the ContextManager; fails
         closed on any drift.
      2. ``selector`` / ``target`` CSS string: used directly as a CSS
         locator for the pinned tab/frame.
      3. anything else: RefResolutionError.

    The returned target always carries a real locator dict, never the
    raw ref integer.
    """
    ref = action.get("ref")
    if isinstance(ref, int):
        version = action.get("ref_version")
        if version is None:
            version = ctx.tab_version(tab_id)
        origin = canonical_origin(page_url) or None
        meta = ctx.resolve_ref(
            ref, version, tab_id=tab_id, frame_id=frame_id,
            origin=origin,
            frame_chain=action.get("frame_chain"),
            shadow_path=action.get("shadow_path"))
        if meta is None:
            raise RefResolutionError(
                f"stale or unresolvable ref {ref} for tab {tab_id} "
                f"(snapshot v{version})")
        locator = dict(meta.get("locator") or {})
        if not locator.get("value"):
            raise RefResolutionError(
                f"ref {ref} resolved but carries no usable locator")
        return ElementTarget(
            session_id=meta.get("session_id", session_id),
            window_id=window_id, tab_id=tab_id,
            frame_id=meta.get("frame_id", frame_id),
            frame_chain=list(meta.get("frame_chain")
                             or [meta.get("frame_id", frame_id)]),
            shadow_path=list(meta.get("shadow_path") or []),
            locator=locator, ref_id=ref,
            snapshot_version=meta.get("snapshot_version"))

    selector = action.get("selector") or action.get("target")
    if isinstance(selector, str) and selector.strip():
        # Explicit CSS addressing for the pinned frame. Frame-chain /
        # shadow-path overrides may still be supplied by the planner.
        return ElementTarget(
            session_id=session_id, window_id=window_id, tab_id=tab_id,
            frame_id=frame_id,
            frame_chain=list(action.get("frame_chain") or [frame_id]),
            shadow_path=list(action.get("shadow_path") or []),
            locator={"strategy": "css", "value": selector.strip()})

    raise RefResolutionError(
        "action carries no resolvable addressing (need int ref or "
        "selector/target string)")
