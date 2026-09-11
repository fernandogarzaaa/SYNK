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

    def run(self, action: dict, page_url: str = "",
                user_consented: bool = False, tab_id: str = "default") -> dict:
        tool = action.get("tool", action.get("action", ""))
        if tool not in TOOL_SCHEMAS:
            res = {"ok": False, "error": f"unknown tool '{tool}'"}
            self.safety.log(action, "denied:unknown-tool")
            return res
        if tool == "bulk":
            return self.run_bulk(action.get("actions", []), page_url, user_consented, tab_id)
        ok, reason = self.safety.validate(
            action, page_url, user_consented, self.paused_for_user)
        self.safety.log(action, reason if not ok else f"executed:{tool}")
        if not ok:
            return {"ok": False, "error": reason}
            
        # Phase 2: Direct CDP Execution
        if self.browser:
            try:
                import asyncio
                # We assume the browser's loop is managed by the server.State
                from .server import STATE
                
                async def execute():
                    if tool == "click":
                        await STATE.browser.click(tab_id, action.get("ref"))
                    elif tool == "type":
                        await STATE.browser.type(tab_id, action.get("ref"), action.get("text"))
                    elif tool == "navigate":
                        await STATE.browser.navigate(tab_id, action.get("url"))
                    return {"ok": True, "command": tool, "args": action}

                future = asyncio.run_coroutine_threadsafe(execute(), STATE.loop)
                return future.result()
            except Exception as e:
                return {"ok": False, "error": f"CDP execution failed: {str(e)}"}

        # Fallback to extension-style command
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
        
        # Phase 2: Direct CDP Bulk Execution
        if self.browser:
            try:
                import asyncio
                from .server import STATE
                
                async def execute_bulk():
                    results = []
                    for a in allowed:
                        tool = a.get("tool")
                        if tool == "click":
                            await STATE.browser.click(tab_id, a.get("ref"))
                        elif tool == "type":
                            await STATE.browser.type(tab_id, a.get("ref"), a.get("text"))
                        elif tool == "navigate":
                            await STATE.browser.navigate(tab_id, a.get("url"))
                        results.append({"tool": tool, "ok": True})
                    return results

                future = asyncio.run_coroutine_threadsafe(execute_bulk(), STATE.loop)
                executed_cmds = future.result()
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

