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
}


class ToolExecutor:
    def __init__(self, safety: SafetyLayer | None = None, browser=None):
        self.safety = safety or SafetyLayer()
        self.browser = browser
        self.paused_for_user = False  # human-priority flag (spec 6)
        self.executed: list[dict] = []


    def set_human_active(self, active: bool) -> None:
        """When the user clicks/types, pause the agent (conflict resolution)."""
        self.paused_for_user = active

    def _use_cdp(self) -> bool:
        try:
            from .server import STATE
            return bool(getattr(STATE, "use_cdp", False) and self.browser is not None
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
        ok, reason = self.safety.validate(
            action, page_url, user_consented, self.paused_for_user)
        self.safety.log(action, reason if not ok else f"executed:{tool}")
        if not ok:
            if reason == "consent_required":
                return {"ok": False, "error": "consent_required",
                        "consent_required": True, "tool": tool}
            return {"ok": False, "error": reason}

        # Phase 2: Direct CDP Execution (only click/type/navigate supported).
        if self._use_cdp():
            if tool not in ("click", "type", "navigate"):
                return {"ok": False,
                        "error": f"cdp_unsupported:{tool} (use extension mode)"}
            try:
                import asyncio
                from .server import STATE

                async def execute():
                    # CDP uses CSS selectors; extension uses selector field.
                    sel = action.get("selector") or action.get("ref")
                    if tool == "click":
                        await STATE.browser.click(tab_id, sel)
                    elif tool == "type":
                        await STATE.browser.type(tab_id, sel, action.get("text", ""))
                    elif tool == "navigate":
                        await STATE.browser.navigate(tab_id, action.get("url", ""))
                    return {"ok": True, "command": tool, "args": action}

                future = asyncio.run_coroutine_threadsafe(execute(), STATE.loop)
                return future.result(timeout=15)
            except Exception as e:
                return {"ok": False, "error": f"CDP execution failed: {str(e)}"}

        # Extension mode: harness validates + queues; content script executes.
        cmd = {"ok": True, "command": tool,
               "args": {k: action.get(k) for k in TOOL_SCHEMAS[tool] if k in action},
               "safety": reason}
        self.executed.append(cmd)
        return cmd


    def run_bulk(self, actions: list[dict], page_url: str = "",
                    user_consented: bool = False, tab_id: str = "default") -> dict:
        allowed, denied = self.safety.validate_bulk(
            actions, page_url=page_url, user_consented=user_consented,
            paused_for_user=self.paused_for_user)

        # Phase 2: Direct CDP Bulk Execution (fail closed on unsupported tools)
        if self._use_cdp():
            unsupported = [a for a in allowed if a.get("tool") not in ("click", "type", "navigate")]
            if unsupported:
                return {"ok": False,
                        "error": f"cdp_unsupported:{[a.get('tool') for a in unsupported]}",
                        "denied": denied + unsupported}
            try:
                import asyncio
                from .server import STATE

                async def execute_bulk():
                    results = []
                    for a in allowed:
                        tool = a.get("tool")
                        sel = a.get("selector") or a.get("ref")
                        if tool == "click":
                            await STATE.browser.click(tab_id, sel)
                        elif tool == "type":
                            await STATE.browser.type(tab_id, sel, a.get("text", ""))
                        elif tool == "navigate":
                            await STATE.browser.navigate(tab_id, a.get("url", ""))
                        results.append({"tool": tool, "ok": True})
                    return results

                future = asyncio.run_coroutine_threadsafe(execute_bulk(), STATE.loop)
                executed_cmds = future.result(timeout=20)
                return {"ok": True, "command": "bulk", "executed": executed_cmds,
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

