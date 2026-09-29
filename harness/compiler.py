"""Stage G task compiler: structured specs -> validated action plans (Phase 5).

A task spec is a STRUCTURED DICT, never free text. The compiler lowers a
spec to a sequence of harness tool actions and validates every action
BEFORE returning the plan:

  1. schema validation (typed errors, never silent drops),
  2. capability check against the tool registry
     (harness.policy.TOOL_ACTION_CLASSES),
  3. per-action argument checks (a "type" without text is rejected),
  4. precondition format checks ("key == value"),
  5. verification checks against the known postcondition kinds
     (harness.verification.evidence.POSTCONDITION_EVIDENCE),
  6. policy validation against the origin policy registry when one is
     supplied (default-deny: unknown origins and denied classes fail the
     compile with a typed error).

Compiled plans enter the task scheduler (harness.task_scheduler) and
execute through the closed-loop execution gateway with per-action
verification; compilation never executes anything itself.
"""
from __future__ import annotations

import re

from .policy import (TOOL_ACTION_CLASSES, OriginPolicyRegistry,
                     POLICY_DENIED, POLICY_CONSENT_REQUIRED)

# -- typed compile errors -------------------------------------------------------
SCHEMA_INVALID = "SCHEMA_INVALID"
UNKNOWN_TOOL = "UNKNOWN_TOOL"
MISSING_PARAM = "MISSING_PARAM"
BAD_PRECONDITION = "BAD_PRECONDITION"
UNKNOWN_VERIFICATION = "UNKNOWN_VERIFICATION"
COMPILE_POLICY_DENIED = "POLICY_DENIED"
COMPILE_CONSENT_REQUIRED = "CONSENT_REQUIRED"


class CompileError(Exception):
    """A typed compilation failure. The plan is rejected, never half-built."""

    def __init__(self, code: str, message: str, *, detail: dict | None = None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.detail = detail or {}

    def to_dict(self) -> dict:
        return {"ok": False, "error_code": self.code,
                "error": self.message, "detail": self.detail}


# tool -> required action args (beyond ref/target identity, which the
# ref resolver supplies at dispatch time)
_TOOL_ARGS = {
    "snapshot": (),
    "click": (),
    "type": ("text",),
    "select": ("value",),
    "press_key": ("key",),
    "navigate": ("url",),
    "back": (),
    "forward": (),
    "hover": (),
    "focus": (),
    "scroll": (),
    "upload": ("path",),
    "summarize": (),
    "ask_user": ("question",),
    "webmcp_invoke": ("tool",),
}

_PRECONDITION_RE = re.compile(r"^[A-Za-z_][\w.]*\s*==\s*.+$")


def _known_verifications() -> set[str]:
    from .verification.evidence import POSTCONDITION_EVIDENCE
    return set(POSTCONDITION_EVIDENCE)


def _validate_action(action: dict, index: int,
                     origin: str | None,
                     policy: OriginPolicyRegistry | None,
                     user_consented: bool,
                     task_id: str) -> dict:
    if not isinstance(action, dict):
        raise CompileError(SCHEMA_INVALID,
                           f"action {index} is not a dict",
                           detail={"index": index})
    tool = action.get("tool")
    if not isinstance(tool, str) or not tool:
        raise CompileError(SCHEMA_INVALID,
                           f"action {index} has no 'tool'",
                           detail={"index": index})
    if tool not in TOOL_ACTION_CLASSES:
        raise CompileError(UNKNOWN_TOOL,
                           f"action {index}: unknown tool '{tool}' "
                           "(not in the capability registry)",
                           detail={"index": index, "tool": tool})
    for arg in _TOOL_ARGS.get(tool, ()):  # tools not listed need no args
        if action.get(arg) in (None, ""):
            raise CompileError(MISSING_PARAM,
                               f"action {index} ({tool}): missing "
                               f"required param '{arg}'",
                               detail={"index": index, "tool": tool,
                                       "param": arg})
    for pi, pre in enumerate(action.get("preconditions", []) or []):
        if not isinstance(pre, str) or not _PRECONDITION_RE.match(pre):
            raise CompileError(BAD_PRECONDITION,
                               f"action {index} precondition {pi} is not "
                               f"'key == value': {pre!r}",
                               detail={"index": index, "precondition": pre})
    known = _known_verifications()
    for vi, ver in enumerate(action.get("verification", []) or []):
        if ver not in known:
            raise CompileError(UNKNOWN_VERIFICATION,
                               f"action {index} verification {vi}: unknown "
                               f"postcondition kind '{ver}' (fail closed)",
                               detail={"index": index, "verification": ver})
    if policy is not None:
        action_class = TOOL_ACTION_CLASSES[tool]
        decision = policy.decide(origin or "", action_class,
                                 user_consented=user_consented)
        if not decision.allowed:
            code = (COMPILE_CONSENT_REQUIRED
                    if decision.error_code == POLICY_CONSENT_REQUIRED
                    else COMPILE_POLICY_DENIED)
            raise CompileError(code,
                               f"action {index} ({tool}): policy denies "
                               f"'{action_class}' on origin "
                               f"'{origin}': {decision.reason}",
                               detail={"index": index, "tool": tool,
                                       "action_class": action_class,
                                       "origin": origin,
                                       "policy_error": decision.error_code})
    out = dict(action)
    out.setdefault("preconditions", [])
    out.setdefault("verification", [])
    return out


_INTENT_TEMPLATES = ("fill_form", "search", "extract", "navigate", "raw")


def _expand_template(spec: dict) -> list[dict]:
    """Lower a task template to raw action dicts (before validation)."""
    task = spec["task"]
    if task == "fill_form":
        ops = []
        for f in spec.get("fields", []):
            if isinstance(f, (list, tuple)):
                # legacy slot shape: (ref, value)
                if len(f) != 2:
                    raise CompileError(SCHEMA_INVALID,
                                       "fill_form 'fields' entries must be "
                                       "(ref, value) pairs or dicts",
                                       detail={"field": list(f)})
                ref, val = f
                ops.append({"tool": "type", "ref": ref,
                            "target": str(ref), "text": val})
                continue
            if not isinstance(f, dict):
                raise CompileError(SCHEMA_INVALID,
                                   "fill_form 'fields' must be a list of "
                                   "dicts or (ref, value) pairs",
                                   detail={"field": f})
            ops.append({"tool": "type", "ref": f.get("ref"),
                        "target": f.get("target", str(f.get("ref"))),
                        "text": f.get("value", f.get("text"))})
        if spec.get("submit_ref") is not None or spec.get("submit_target"):
            ops.append({"tool": "click",
                        "ref": spec.get("submit_ref"),
                        "target": spec.get("submit_target",
                                           str(spec.get("submit_ref")))})
        return ops
    if task == "search":
        return [{"tool": "type", "ref": spec.get("query_ref"),
                 "target": spec.get("query_target",
                                   str(spec.get("query_ref"))),
                 "text": spec.get("query", "")},
                {"tool": "click", "ref": spec.get("submit_ref"),
                 "target": spec.get("submit_target",
                                   str(spec.get("submit_ref")))}]
    if task == "extract":
        return [{"tool": "snapshot"}]
    if task == "navigate":
        return [{"tool": "navigate", "url": spec.get("url")}]
    if task == "raw":
        actions = spec.get("actions", [])
        if not isinstance(actions, list):
            raise CompileError(SCHEMA_INVALID,
                               "'actions' must be a list",
                               detail={"task": task})
        return list(actions)
    raise CompileError(SCHEMA_INVALID,
                       f"unknown task template '{task}'",
                       detail={"known": list(_INTENT_TEMPLATES)})


def compile_spec(spec: dict, *,
                 policy: OriginPolicyRegistry | None = None,
                 task_id: str = "compile") -> dict:
    """Compile a structured task spec to a validated action plan.

    Raises CompileError (typed, never silent) on any invalid spec, unknown
    tool, bad precondition/verification, or policy denial.
    """
    if not isinstance(spec, dict):
        raise CompileError(SCHEMA_INVALID,
                           "task spec must be a structured dict, not "
                           f"{type(spec).__name__} (free text is rejected)")
    if "task" not in spec:
        raise CompileError(SCHEMA_INVALID,
                           "task spec must declare 'task'",
                           detail={"known": list(_INTENT_TEMPLATES)})
    origin = spec.get("origin")
    if policy is not None and not origin:
        # Fail closed: with a default-deny policy, an origin-less plan can
        # never validate, so reject it at compile time.
        raise CompileError(SCHEMA_INVALID,
                           "task spec must declare 'origin' when a policy "
                           "registry is configured")
    user_consented = bool(spec.get("user_consented", False))
    page_url = spec.get("page_url", "")
    actions = _expand_template(spec)
    if not actions:
        raise CompileError(SCHEMA_INVALID,
                           "compiled plan is empty: nothing to execute")
    validated = [_validate_action(a, i, origin, policy, user_consented,
                                  task_id)
                 for i, a in enumerate(actions)]
    plan_pre = spec.get("preconditions", []) or []
    for pi, pre in enumerate(plan_pre):
        if not isinstance(pre, str) or not _PRECONDITION_RE.match(pre):
            raise CompileError(BAD_PRECONDITION,
                               f"plan precondition {pi} is not "
                               f"'key == value': {pre!r}",
                               detail={"precondition": pre})
    plan_ver = spec.get("verification", []) or []
    known = _known_verifications()
    for vi, ver in enumerate(plan_ver):
        if ver not in known:
            raise CompileError(UNKNOWN_VERIFICATION,
                               f"plan verification {vi}: unknown "
                               f"postcondition kind '{ver}' (fail closed)",
                               detail={"verification": ver})
    return {"ok": True,
            "task": spec["task"],
            "origin": origin,
            "page_url": page_url,
            "user_consented": user_consented,
            "preconditions": list(plan_pre),
            "verification": list(plan_ver),
            "actions": validated,
            "policy_checked": policy is not None,
            "action_count": len(validated)}


# -- legacy compat ---------------------------------------------------------------
# compile_intent / ir_to_actions predate the validating compiler. They are
# preserved for older callers and now delegate to compile_spec WITHOUT a
# policy registry; the returned plan is honestly flagged policy_checked=False.

INTENT_TEMPLATES = ("fill_form", "search", "extract", "navigate_task")


def compile_intent(intent: str, slots: dict, world=None) -> dict:
    spec = {"task": intent, **(slots or {})}
    if intent == "navigate_task":
        spec = {"task": "navigate", "url": (slots or {}).get("url")}
    return compile_spec(spec)


def ir_to_actions(ir: dict) -> list[dict]:
    """Lower a compiled plan (or legacy IR dict) to harness tool actions."""
    if isinstance(ir, dict) and ir.get("actions") and \
            isinstance(ir["actions"][0], dict) and "tool" in ir["actions"][0]:
        return [dict(a) for a in ir["actions"]]
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
