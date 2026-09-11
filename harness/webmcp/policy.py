"""Policy engine: capability execution requires ownership, conflict, and consent checks.

This enforces deterministic safety on top of site-exposed tools. Even if a site
exposes "deleteAccount()", the harness decides whether the agent can invoke it.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .registry import Capability, REGISTRY
from ..concurrency import OwnershipGraph, CONFLICT, HUMAN_OWNED
from ..safety import SafetyLayer


@dataclass
class PolicyDecision:
    allowed: bool
    reason: str
    capability: Capability | None = None
    requires_consent: bool = False
    requires_ownership: bool = False


class PolicyEngine:
    """Evaluates whether a capability can be executed given current state."""

    def __init__(self, ownership: OwnershipGraph, safety: SafetyLayer):
        self.ownership = ownership
        self.safety = safety

    def check(self, capability: Capability, goal: str = "",
              user_consented: bool = False, agent_lease: str | None = None) -> PolicyDecision:
        # 1. Risk-based consent requirement
        if capability.requires_user_consent and not user_consented:
            return PolicyDecision(
                False, f"capability '{capability.name}' requires user consent",
                capability, requires_consent=True)

        # 2. Ownership/conflict check on the target resource
        # For WebMCP tools, the "target" is often implicit in the arguments
        # (e.g., an account ID). We check ownership on the origin as a proxy.
        target = capability.origin
        state = self.ownership.state_of(target)
        if state == CONFLICT:
            return PolicyDecision(
                False, f"ownership conflict on {target}",
                capability, requires_ownership=True)
        if state == HUMAN_OWNED:
            # Human is interacting with this origin
            return PolicyDecision(
                False, f"human owns {target}; agent must replan",
                capability, requires_ownership=True)

        # 3. Safety layer validation (destructive patterns, domain allowlist, etc.)
        # For WebMCP tools, we skip the DOM tool allowlist check since capabilities
        # have their own risk classification. We only check domain allowlist and
        # destructive patterns on the serialized action.
        action = {"tool": capability.name, "args": capability.schema,
                  "target": target, "intent": goal}
        # Use a modified safety check that skips tool allowlist for WebMCP
        if capability.source == "webmcp":
            ok, reason = self._safety_check_webmcp(action, f"https://{target}",
                                                   user_consented, paused_for_user=False)
        else:
            ok, reason = self.safety.validate(action, f"https://{target}",
                                               user_consented, paused_for_user=False)
        if not ok:
            return PolicyDecision(False, f"safety: {reason}", capability)

        # 4. Lease check (if agent has a lease on this origin)
        if agent_lease:
            # In a real impl, we'd verify the lease matches
            pass

        return PolicyDecision(True, "allowed", capability)

    def _safety_check_webmcp(self, action: dict, page_url: str,
                              user_consented: bool, paused_for_user: bool) -> tuple[bool, str]:
        """Safety check for WebMCP tools: skips DOM tool allowlist, keeps other checks."""
        if paused_for_user:
            return False, "denied: human is interacting - agent paused (human priority)"
        # Check domain allowlist
        from ..safety import SafetyConfig, DESTRUCTIVE_PATTERNS
        config = SafetyConfig()
        if config.allowed_domains and not any(d in page_url for d in config.allowed_domains):
            return False, f"denied: navigation outside allowlist ({page_url})"
        # Check destructive patterns
        blob = f"{action.get('tool','')} {action.get('args', action)}"
        if config.consent_for_destructive and DESTRUCTIVE_PATTERNS.search(blob):
            if not user_consented:
                return False, "denied: destructive action requires explicit user consent"
        return True, "allowed"


class CapabilityPolicy:
    """High-level policy: which capabilities are available to which actors."""

    def __init__(self):
        # Per-origin capability allow/deny lists
        self.allowed_origins: set[str] = set()  # empty = all allowed
        self.denied_origins: set[str] = set()
        self.allowed_sources: set[str] = {"webmcp", "dom", "primitive", "vision"}
        self.max_risk_without_consent: str = "medium"

    def is_allowed(self, cap: Capability) -> bool:
        if cap.origin in self.denied_origins:
            return False
        if self.allowed_origins and cap.origin not in self.allowed_origins:
            return False
        if cap.source not in self.allowed_sources:
            return False
        return True

    def set_origin_allowed(self, origin: str, allowed: bool) -> None:
        if allowed:
            self.allowed_origins.add(origin)
            self.denied_origins.discard(origin)
        else:
            self.denied_origins.add(origin)
            self.allowed_origins.discard(origin)

    def set_source_allowed(self, source: str, allowed: bool) -> None:
        if allowed:
            self.allowed_sources.add(source)
        else:
            self.allowed_sources.discard(source)


# Global policy instance
CAPABILITY_POLICY = CapabilityPolicy()
