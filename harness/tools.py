"""Tool executor: fixed API of browser actions with validation (spec 4, 6).

Bulk actions supported: {"tool":"bulk","actions":[...]} fills a whole form
in one LLM round-trip (spec 8: -74% tool calls).
"""
from __future__ import annotations

from .safety import SafetyLayer
TOOL_SCHEMAS = {
    "click": ["ref"],
    "type": ["ref", "text"],
    "select": ["ref", "value"],
    "scroll": ["direction"],
    "navigate": ["url"],
    "back": [],
    "forward": [],
    "hover": ["ref"],
    "focus": ["ref"],
    "press_key": ["key"],
    "upload": ["ref", "path"],
    "snapshot": [],
    "ask_user": ["question"],
    "summarize": ["text"],
    "bulk": ["actions"],
    # Stage E: WebMCP tool invocation through the page's model context.
    # "args" is optional (defaults to {}); identity (tab/session/frame)
    # is stamped by the gateway/engine, never optional at dispatch.
    "webmcp_invoke": ["tool_name"],
}


def _resolve_target(action: dict, tab_id: str, page_url: str = ""):
    """Resolve an action's addressing to an ElementTarget (Stage D).

    Raises RuntimeError on unresolvable refs so the CDP path fails closed
    instead of feeding an opaque ref integer to the browser.
    """
    from .ref_resolver import resolve_action_target, RefResolutionError
    from .server import STATE
    try:
        return resolve_action_target(
            action, STATE.ctx, tab_id=tab_id,
            session_id=action.get("session_id"),
            window_id=action.get("window_id", "win_default"),
            frame_id=action.get("frame_id", "main"),
            page_url=page_url)
    except RefResolutionError as e:
        raise RuntimeError(f"stale_ref: {e}")


def _dispatch_runtime(runtime, tool: str, target, action: dict,
                      tab_id: str) -> dict:
    """Dispatch one tool through a connected BrowserRuntime.

    Returns a plain dict; the BrowserAck inside carries executed=True
    only together with its observation (honest ack semantics).
    """
    aid = action.get("action_id")
    if tool == "click":
        ack = runtime.click(target, action_id=aid)
    elif tool == "type":
        ack = runtime.type(target, action.get("text", ""), action_id=aid)
    elif tool == "select":
        ack = runtime.select(target, action.get("value", ""), action_id=aid)
    elif tool == "navigate":
        ack = runtime.navigate(action.get("url", ""), tab_id=tab_id,
                               action_id=aid)
    elif tool == "scroll":
        ack = runtime.scroll(tab_id=tab_id,
                             direction=action.get("direction", "down"),
                             action_id=aid)
    elif tool == "press_key":
        ack = runtime.keypress(target, action.get("key", "Enter"),
                               action_id=aid)
    else:
        raise RuntimeError(f"cdp_unsupported:{tool}")
    return {"ok": ack.executed, "command": tool, "args": action,
            "ack": ack.to_dict(), "error": ack.error,
            "error_code": ack.error_code}


class ToolExecutor:
    def __init__(self, safety: SafetyLayer | None = None, browser=None):
        self.safety = safety or SafetyLayer()
        self.browser = browser
        self.paused_for_user = False  # human-priority flag (spec 6)
        self.executed: list[dict] = []
        # Stage E: the real WebMCP gateway (wired by server STATE; None in
        # unit tests unless set). verifier likewise (falls back to STATE).
        self.webmcp_gateway = None
        self.verifier = None


    def set_human_active(self, active: bool) -> None:
        """When the user clicks/types, pause the agent (conflict resolution)."""
        self.paused_for_user = active

    def _use_cdp(self) -> bool:
        try:
            from .server import STATE
            owned = bool(getattr(STATE, "use_cdp", False)
                         or getattr(STATE, "cdp_endpoint", ""))
            return bool(owned and self.browser is not None
                        and getattr(self.browser, "available", False))
        except Exception:
            return False

    def run(self, action: dict, page_url: str = "",
                user_consented: bool = False, tab_id: str = "default") -> dict:
        tool = action.get("tool", action.get("action", ""))
        if tool not in TOOL_SCHEMAS:
            res = {"ok": False, "error": "denied:unknown-tool"}
            self.safety.log(action, "denied:unknown-tool")
            return res
        if tool == "bulk":
            return self.run_bulk(action.get("actions", []), page_url, user_consented, tab_id)
        if tool == "webmcp_invoke":
            # Stage E: WebMCP invocations go through the real gateway
            # (page model-context discovery, handle scoping, policy), never
            # through DOM dispatch. Safety allowlist still applies first.
            ok, reason = self.safety.validate(
                action, page_url, user_consented, self.paused_for_user)
            self.safety.log(action, reason if not ok else "executed:webmcp_invoke")
            if not ok:
                if reason == "consent_required":
                    return {"ok": False, "error": "consent_required",
                            "consent_required": True, "tool": tool}
                return {"ok": False, "error": reason}
            return self._run_webmcp(action, page_url, user_consented, tab_id)
        ok, reason = self.safety.validate(
            action, page_url, user_consented, self.paused_for_user)
        self.safety.log(action, reason if not ok else f"executed:{tool}")
        if not ok:
            if reason == "consent_required":
                return {"ok": False, "error": "consent_required",
                        "consent_required": True, "tool": tool}
            return {"ok": False, "error": reason}

        # Stage D: CDP execution routes through the RefResolver -- an opaque
        # agent ref is NEVER stringified into a selector (the old
        # `sel = action.get("selector") or action.get("ref")` fed
        # page.click("3") for ref=3). Unresolvable refs fail closed here.
        if self._use_cdp():
            if tool not in ("click", "type", "navigate", "select",
                            "scroll", "press_key"):
                return {"ok": False,
                        "error": f"cdp_unsupported:{tool} (use extension mode)"}
            try:
                import asyncio
                from .server import STATE
                target = _resolve_target(action, tab_id, page_url)

                async def execute():
                    runtime = getattr(STATE, "browser_runtime", None)
                    if runtime is not None and runtime.connected:
                        return await asyncio.to_thread(
                            _dispatch_runtime, runtime, tool, target,
                            action, tab_id)
                    # Fallback: legacy shim with the RESOLVED locator
                    # (never the raw ref).
                    sel = target.locator.get("value", "")
                    if tool == "click":
                        await STATE.browser.click(tab_id, sel)
                    elif tool == "type":
                        await STATE.browser.type(tab_id, sel,
                                                 action.get("text", ""))
                    elif tool == "navigate":
                        await STATE.browser.navigate(tab_id,
                                                     action.get("url", ""))
                    else:
                        raise RuntimeError(
                            f"cdp_unsupported:{tool} without a connected "
                            f"BrowserRuntime")
                    return {"ok": True, "command": tool, "args": action,
                            "via": "shim", "selector": sel}

                future = asyncio.run_coroutine_threadsafe(execute(), STATE.loop)
                return future.result(timeout=30)
            except Exception as e:
                return {"ok": False, "error": f"CDP execution failed: {str(e)}"}

        # Extension mode: harness validates + queues; content script executes.
        cmd = {"ok": True, "command": tool,
               "args": {k: action.get(k) for k in TOOL_SCHEMAS[tool] if k in action},
               "safety": reason}
        self.executed.append(cmd)
        return cmd

    def _run_webmcp(self, action: dict, page_url: str,
                    user_consented: bool, tab_id: str) -> dict:
        """Execute a webmcp_invoke action through the WebMCPGateway.

        Records the page's tool report as WEBMCP_RESULT evidence so the
        transaction's ``webmcp_result`` postcondition can be verified.
        Fixture-fallback results are flagged ``partial_fallback`` in the
        payload -- the verifier refuses them outright.
        """
        import time as _time
        import uuid as _uuid
        tool_name = action.get("tool_name", "")
        args = action.get("args") or {}
        gateway = self.webmcp_gateway
        if gateway is None:
            try:
                from .server import STATE
                gateway = getattr(STATE, "webmcp_gateway", None)
            except Exception:
                gateway = None
        if gateway is None:
            return {"ok": False, "tool": "webmcp_invoke",
                    "error": "webmcp_unavailable: no WebMCP gateway wired",
                    "error_code": "BROWSER_NOT_READY"}
        scope = gateway.scope_for(action.get("session_id"), tab_id,
                                  action.get("frame_id", "main"))
        inv = gateway.invoke(tool_name, args, scope,
                             task_id=action.get("task_id"),
                             action_id=action.get("action_id"),
                             user_consented=user_consented,
                             goal=action.get("intent", ""))
        res = inv.result
        # Evidence: the page's own tool report. Recorded even on failure
        # (ok=False results verify as FAILED, never as silent success).
        verifier = self.verifier
        if verifier is None:
            try:
                from .server import STATE
                verifier = getattr(STATE, "verifier", None)
            except Exception:
                verifier = None
        if verifier is not None:
            try:
                from .verification.evidence import (WEBMCP_RESULT, Evidence,
                                                   strength_of)
                verifier.record_evidence(Evidence(
                    evidence_id=f"e_{_uuid.uuid4().hex[:8]}",
                    evidence_type=WEBMCP_RESULT,
                    source="webmcp", timestamp=_time.time(),
                    action_id=action.get("action_id"),
                    task_id=action.get("task_id"),
                    payload={"tool": tool_name, "ok": res.ok,
                             "result": res.result, "error": res.error,
                             "partial_fallback": inv.partial,
                             "handle_id": (inv.handle.handle_id
                                           if inv.handle else None)},
                    strength=strength_of(WEBMCP_RESULT),
                    provenance=("fixture_fallback" if inv.partial
                                else "page_model_context"),
                ))
            except Exception:
                pass
        out = {"ok": res.ok, "command": "webmcp_invoke", "args": action,
               "tool_result": res.to_dict(),
               "partial_fallback": inv.partial,
               "error": res.error,
               "error_code": inv.error_code
               or (None if res.ok else "ACTION_FAILED")}
        if inv.caveat:
            out["caveat"] = inv.caveat
        self.safety.log(action,
                        f"webmcp:{tool_name}:{'ok' if res.ok else 'failed'}"
                        f"{':partial' if inv.partial else ''}")
        return out

    def run_bulk(self, actions: list[dict], page_url: str = "",
                    user_consented: bool = False, tab_id: str = "default") -> dict:
        allowed, denied = self.safety.validate_bulk(
            actions, page_url=page_url, user_consented=user_consented,
            paused_for_user=self.paused_for_user)

        # Stage D: CDP bulk execution, ref-safe like run().
        if self._use_cdp():
            supported = ("click", "type", "navigate", "select", "scroll",
                         "press_key")
            unsupported = [a for a in allowed
                           if a.get("tool") not in supported]
            if unsupported:
                return {"ok": False,
                        "error": f"cdp_unsupported:{[a.get('tool') for a in unsupported]}",
                        "denied": denied + unsupported}
            try:
                import asyncio
                from .server import STATE

                async def execute_bulk():
                    runtime = getattr(STATE, "browser_runtime", None)
                    results = []
                    for a in allowed:
                        tool = a.get("tool")
                        target = _resolve_target(a, tab_id, page_url)
                        if runtime is not None and runtime.connected:
                            res = await asyncio.to_thread(
                                _dispatch_runtime, runtime, tool, target,
                                a, tab_id)
                        else:
                            sel = target.locator.get("value", "")
                            if tool == "click":
                                await STATE.browser.click(tab_id, sel)
                            elif tool == "type":
                                await STATE.browser.type(tab_id, sel,
                                                         a.get("text", ""))
                            elif tool == "navigate":
                                await STATE.browser.navigate(
                                    tab_id, a.get("url", ""))
                            else:
                                raise RuntimeError(
                                    f"cdp_unsupported:{tool} without a "
                                    f"connected BrowserRuntime")
                            res = {"ok": True, "tool": tool, "via": "shim",
                                   "selector": sel}
                        results.append({"tool": tool, "ok": res.get("ok"),
                                        "error": res.get("error")})
                        if not res.get("ok"):
                            break  # stop the batch on first failure
                    return results

                future = asyncio.run_coroutine_threadsafe(execute_bulk(), STATE.loop)
                executed_cmds = future.result(timeout=30)
                ok_all = all(r.get("ok") for r in executed_cmds)
                return {"ok": ok_all, "command": "bulk",
                        "executed": executed_cmds,
                        "denied": denied, "savings_note": "CDP bulk execution"}
            except Exception as e:
                return {"ok": False, "error": f"CDP bulk failed: {str(e)}"}

        for a in allowed:
            self.safety.log(a, "executed:bulk-child")
        for a in denied:
            self.safety.log(a, a.get("_safety", "denied"))
        cmds = [{"command": a.get("tool"),
                 "args": {k: a.get(k) for k in TOOL_SCHEMAS.get(a.get("tool",""), [])
                          if k in a}} for a in allowed]
        self.executed.append({"command": "bulk", "count": len(cmds)})
        return {"ok": True, "command": "bulk", "executed": cmds,
                "denied": denied,
                "savings_note": f"1 LLM call instead of {len(actions)} "
                                 f"({len(actions)-1} round-trips saved)"}

