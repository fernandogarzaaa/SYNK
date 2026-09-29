"""Origin policy registry: per-site execution policy enforced at runtime (Stage F).

This is the REAL security policy framework, not a README policy. Every
action passes through ``TransactionEngine._validate_reserve_precondition``
at the VALIDATE stage, BEFORE any lease is acquired and BEFORE any
dispatch. The registry decides per (origin, action-class):

* unknown origins fail closed (default-deny);
* a missing/undeterminable origin abstains (legacy safety path, e.g.
  unit tests with no page context);
* known origins default-deny any action class not explicitly allowed.

Action classes (least privilege):
    read      snapshot, summarize, ask_user (observe only)
    navigate  navigate, back, forward
    interact  click, type, select, hover, focus, press_key (DOM mutation)
    write     upload (file exfiltration risk)
    webmcp    webmcp_invoke (page model-context tools)

Capability privilege model: a capability is bound to the principal
(session) that discovered it plus an origin and trust level
(page-advertised / operator-verified / operator-trusted). The closed-loop
agent cannot escalate from a read-only capability to a mutating one
without a FRESH policy decision: ``check_action`` tracks the highest
action-class rank approved per (task, origin) and, on an escalation
attempt, records a fresh ``policy.escalation_review`` decision in the
audit trail before allowing or denying.

Trust levels:
    page-advertised   the page claimed it (untrusted by default)
    operator-verified the operator confirmed the tool exists and is benign
    operator-trusted  the operator fully trusts this origin's tools
"""
from __future__ import annotations

import hashlib
import time
from dataclasses import dataclass, field

# typed policy outcomes (surfaced through the transaction error taxonomy)
POLICY_DENIED = "POLICY_DENIED"
POLICY_CONSENT_REQUIRED = "CONSENT_REQUIRED"
POLICY_ABSTAIN = "POLICY_ABSTAIN"

TRUST_LEVELS = ("page-advertised", "operator-verified", "operator-trusted")

# DOM tool -> action class.
TOOL_ACTION_CLASSES = {
    "snapshot": "read", "summarize": "read", "ask_user": "read",
    "navigate": "navigate", "back": "navigate", "forward": "navigate",
    "click": "interact", "type": "interact", "select": "interact",
    "hover": "interact", "focus": "interact", "press_key": "interact",
    "upload": "write",
    "webmcp_invoke": "webmcp",
    "bulk": "bulk",  # expanded per sub-action before policy checks
}

# Escalation ranking: higher rank = more privilege.
CLASS_RANK = {"read": 0, "navigate": 1, "interact": 2, "webmcp": 2,
              "write": 3}


def origin_of_url(url: str) -> str:
    """Canonical origin host for a URL; "" when undeterminable."""
    try:
        host = (url or "").split("//", 1)[1].split("/", 1)[0].lower()
        return host.split(":", 1)[0]
    except (IndexError, AttributeError):
        return ""


@dataclass
class PolicyDecision:
    allowed: bool
    reason: str
    error_code: str = POLICY_DENIED
    requires_consent: bool = False
    origin: str = ""
    action_class: str = ""
    trust_level: str = "page-advertised"
    escalation: bool = False

    def to_dict(self) -> dict:
        return {"allowed": self.allowed, "reason": self.reason,
                "error_code": self.error_code,
                "requires_consent": self.requires_consent,
                "origin": self.origin, "action_class": self.action_class,
                "trust_level": self.trust_level,
                "escalation": self.escalation}


@dataclass
class OriginPolicy:
    """One origin's entry: explicit allow/deny/consent sets."""
    origin: str
    allow: set = field(default_factory=set)           # action classes
    deny: set = field(default_factory=set)
    require_consent: set = field(default_factory=set)
    min_trust: str = "page-advertised"                # lowest accepted level
    description: str = ""
    updated: float = field(default_factory=time.time)

    def policy_id(self) -> str:
        canon = f"{self.origin}|{sorted(self.allow)}|{sorted(self.deny)}"
        return "pol_" + hashlib.sha256(canon.encode()).hexdigest()[:12]


class OriginPolicyRegistry:
    """Per-origin execution policy with default-deny and escalation audit.

    ``emit`` is an optional callable(type, data) for audit-trail fan-out
    (the server passes bus.emit). All decisions are also returned as
    ``PolicyDecision`` records the caller logs.
    """

    def __init__(self, emit=None):
        self._origins: dict[str, OriginPolicy] = {}
        self.emit = emit or (lambda t, d: None)
        # (task_id, origin) -> highest approved action-class rank.
        self._task_levels: dict[tuple[str, str], int] = {}

    # -- management ------------------------------------------------------------
    def register_origin(self, origin: str, *, allow=(), deny=(),
                        require_consent=(), min_trust="page-advertised",
                        description="") -> OriginPolicy:
        origin = (origin or "").strip().lower()
        if not origin:
            raise ValueError("origin is required")
        if min_trust not in TRUST_LEVELS:
            raise ValueError(f"unknown trust level {min_trust!r}")
        for name, vals in (("allow", allow), ("deny", deny),
                           ("require_consent", require_consent)):
            if isinstance(vals, str) or not all(
                    isinstance(v, str) for v in vals):
                raise ValueError(f"{name} must be a list of action-class "
                                 f"strings")
        pol = OriginPolicy(origin=origin, allow=set(allow), deny=set(deny),
                           require_consent=set(require_consent),
                           min_trust=min_trust, description=description)
        self._origins[origin] = pol
        self.emit("policy.origin_registered",
                  {"origin": origin, "policy_id": pol.policy_id(),
                   "allow": sorted(pol.allow), "deny": sorted(pol.deny),
                   "require_consent": sorted(pol.require_consent),
                   "min_trust": min_trust})
        return pol

    def remove_origin(self, origin: str) -> bool:
        origin = (origin or "").strip().lower()
        existed = self._origins.pop(origin, None) is not None
        if existed:
            self.emit("policy.origin_removed", {"origin": origin})
        return existed

    def get_origin(self, origin: str) -> OriginPolicy | None:
        return self._origins.get((origin or "").strip().lower())

    def snapshot(self) -> dict:
        return {o: {"policy_id": p.policy_id(),
                    "allow": sorted(p.allow), "deny": sorted(p.deny),
                    "require_consent": sorted(p.require_consent),
                    "min_trust": p.min_trust,
                    "description": p.description}
                for o, p in self._origins.items()}

    # -- decisions -----------------------------------------------------------------
    @staticmethod
    def action_class_for(tool: str) -> str:
        return TOOL_ACTION_CLASSES.get(tool, "interact")

    def decide(self, origin: str, action_class: str, *,
               trust_level: str = "page-advertised",
               user_consented: bool = False) -> PolicyDecision:
        """One-shot decision WITHOUT escalation tracking (use check_action
        for the full path). Returns an ABSTAIN decision when the origin
        cannot be determined; DENY for unknown origins."""
        origin = (origin or "").strip().lower()
        if not origin:
            return PolicyDecision(
                allowed=True, reason="policy abstains: no page origin "
                                     "(legacy safety path)",
                error_code=POLICY_ABSTAIN, origin="", action_class=action_class,
                trust_level=trust_level)
        pol = self._origins.get(origin)
        if pol is None:
            return PolicyDecision(
                allowed=False,
                reason=f"policy denies: unknown origin '{origin}' "
                       "(default-deny; register the origin first)",
                error_code=POLICY_DENIED, origin=origin,
                action_class=action_class, trust_level=trust_level)
        if TRUST_LEVELS.index(trust_level) < TRUST_LEVELS.index(pol.min_trust):
            return PolicyDecision(
                allowed=False,
                reason=f"policy denies: trust level '{trust_level}' below "
                       f"minimum '{pol.min_trust}' for '{origin}'",
                error_code=POLICY_DENIED, origin=origin,
                action_class=action_class, trust_level=trust_level)
        if action_class in pol.deny:
            return PolicyDecision(
                allowed=False,
                reason=f"policy denies: action class '{action_class}' is "
                       f"denied for '{origin}'",
                error_code=POLICY_DENIED, origin=origin,
                action_class=action_class, trust_level=trust_level)
        if action_class in pol.require_consent and not user_consented:
            return PolicyDecision(
                allowed=False, requires_consent=True,
                reason=f"policy requires consent: '{action_class}' on "
                       f"'{origin}'",
                error_code=POLICY_CONSENT_REQUIRED, origin=origin,
                action_class=action_class, trust_level=trust_level)
        if action_class in pol.allow:
            return PolicyDecision(
                allowed=True, reason=f"policy allows '{action_class}' on "
                                     f"'{origin}'",
                error_code="", origin=origin, action_class=action_class,
                trust_level=trust_level)
        return PolicyDecision(
            allowed=False,
            reason=f"policy denies: '{action_class}' not in allow set for "
                   f"'{origin}' (default-deny)",
            error_code=POLICY_DENIED, origin=origin,
            action_class=action_class, trust_level=trust_level)

    def check_action(self, task_id: str, origin: str, action_class: str, *,
                     trust_level: str = "page-advertised",
                     user_consented: bool = False,
                     tool_name: str = "") -> PolicyDecision:
        """Full per-action check: base decision + escalation review.

        A task that previously only used read-level capabilities and now
        attempts a mutating class triggers a FRESH recorded policy
        decision (policy.escalation_review) before the outcome. Allowed
        decisions record the task's privilege level.
        """
        decision = self.decide(origin, action_class,
                               trust_level=trust_level,
                               user_consented=user_consented)
        if not decision.allowed or not origin:
            return decision
        key = (task_id or "", origin.strip().lower())
        rank = CLASS_RANK.get(action_class, 2)
        prev = self._task_levels.get(key, -1)
        if rank > prev:
            # Escalation (or first use): record a fresh decision.
            decision.escalation = True
            self.emit("policy.escalation_review",
                      {"task_id": task_id, "origin": origin,
                       "action_class": action_class,
                       "tool_name": tool_name,
                       "previous_rank": prev, "new_rank": rank,
                       "decision": decision.to_dict(),
                       "fresh_decision": True})
            if decision.allowed:
                self._task_levels[key] = rank
        elif decision.allowed:
            self._task_levels[key] = max(prev, rank)
        return decision

    def reset_task_levels(self, task_id: str) -> None:
        for k in [k for k in self._task_levels if k[0] == task_id]:
            del self._task_levels[k]
