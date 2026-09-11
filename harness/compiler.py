"""Action compiler: LLM intent -> deterministic Browser IR (Beta §8).

One model response drives many deterministic browser operations:

  LLM -> Intent -> Planner -> Action Compiler -> Browser IR -> Validator -> Executor

The Alpha `bulk` tool is the seed; IR adds preconditions + verification so a
compiled transaction is atomic-ish: validate all preconditions first, execute,
then verify. Anything failing validation returns replan, never partial chaos.
"""
from __future__ import annotations

INTENT_TEMPLATES = ("fill_form", "search", "extract", "navigate_task")


def compile_intent(intent: str, slots: dict, world=None) -> dict:
    """Compile a high-level intent + slots into Browser IR."""
    if intent == "fill_form":
        ops = [["fill", ref, val] for ref, val in slots.get("fields", [])]
        if slots.get("submit_ref") is not None:
            ops.append(["click", slots["submit_ref"]])
        return {
            "transaction": "form_fill",
            "preconditions": [f"page.url == {slots.get('page_url', '?')}"],
            "operations": ops,
            "verification": ["all_required_fields_valid", "no_modal_blocking"],
        }
    if intent == "search":
        return {
            "transaction": "search",
            "preconditions": [f"page.url == {slots.get('page_url', '?')}"],
            "operations": [["fill", slots.get("query_ref"), slots.get("query", "")],
                           ["click", slots.get("submit_ref")]],
            "verification": ["no_modal_blocking"],
        }
    if intent == "extract":
        return {
            "transaction": "extract",
            "preconditions": [],
            "operations": [["snapshot"]],
            "verification": [],
            "extract_selectors": slots.get("selectors", []),
        }
    # fallback: pass through single ops
    return {"transaction": "raw",
            "preconditions": slots.get("preconditions", []),
            "operations": slots.get("operations", []),
            "verification": slots.get("verification", [])}


def ir_to_actions(ir: dict) -> list[dict]:
    """Lower Browser IR operations to harness tool actions."""
    out = []
    for op in ir.get("operations", []):
        kind = op[0]
        if kind == "fill":
            out.append({"tool": "type", "ref": op[1], "text": op[2],
                        "target": str(op[1])})
        elif kind == "click":
            out.append({"tool": "click", "ref": op[1], "target": str(op[1])})
        elif kind == "select":
            out.append({"tool": "select", "ref": op[1], "value": op[2],
                        "target": str(op[1])})
        elif kind == "snapshot":
            out.append({"tool": "snapshot"})
    for a in out:
        a["preconditions"] = ir.get("preconditions", [])
        a["verification"] = ir.get("verification", [])
    return out
