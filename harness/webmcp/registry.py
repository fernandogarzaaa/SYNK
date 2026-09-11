"""Capability Registry: discovered tools normalized as capabilities with risk/policy metadata.

This is NOT a trusted command layer. Every capability must pass ownership, conflict,
and policy checks before execution, even if the site exposes a "deleteAccount" tool.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any
import time

from .schema import ToolAnnotation, WebMCPTool


RISK_ORDER = {"low": 0, "medium": 1, "high": 2, "critical": 3}
LATENCY_CLASSES = ("fast", "medium", "slow")


@dataclass
class Capability:
    source: str                    # "webmcp" | "dom" | "primitive" | "vision" | "human"
    origin: str                    # site origin (e.g. "example.com")
    name: str                      # tool/action name
    risk: str                      # "low" | "medium" | "high" | "critical"
    latency_class: str             # "fast" | "medium" | "slow"
    semantic: bool = False         # true for semantic/site-native tools
    mutating: bool = False         # writes state
    requires_user_consent: bool = False
    annotations: ToolAnnotation = field(default_factory=ToolAnnotation)
    schema: dict[str, Any] = field(default_factory=dict)
    description: str = ""
    # execution metadata
    last_used: float = 0.0
    success_count: int = 0
    failure_count: int = 0

    @classmethod
    def from_webmcp(cls, origin: str, tool: WebMCPTool) -> "Capability":
        risk = "low"
        if tool.is_destructive:
            risk = "critical"
        elif tool.is_read_only:
            risk = "low"
        elif tool.input_schema.get("type") == "object" and "id" in str(tool.input_schema):
            risk = "medium"  # likely mutates specific resource
        return cls(
            source="webmcp",
            origin=origin,
            name=tool.name,
            risk=risk,
            latency_class="fast",  # WebMCP is direct API call
            semantic=True,
            mutating=not tool.is_read_only,
            requires_user_consent=tool.is_destructive or risk in ("high", "critical"),
            annotations=tool.annotations,
            schema=tool.input_schema,
            description=tool.description,
        )

    @classmethod
    def from_dom(cls, origin: str, name: str, risk: str = "low",
                 mutating: bool = False, description: str = "") -> "Capability":
        return cls(
            source="dom",
            origin=origin,
            name=name,
            risk=risk,
            latency_class="medium",
            semantic=True,
            mutating=mutating,
            requires_user_consent=risk in ("high", "critical") or mutating,
            description=description,
        )

    def __lt__(self, other: "Capability") -> bool:
        # sort by risk (lower first) then latency
        return (RISK_ORDER.get(self.risk, 99), LATENCY_CLASSES.index(self.latency_class)) < \
               (RISK_ORDER.get(other.risk, 99), LATENCY_CLASSES.index(other.latency_class))

    @property
    def reliability(self) -> float:
        total = self.success_count + self.failure_count
        if total == 0:
            return 1.0
        return self.success_count / total

    def record_result(self, ok: bool) -> None:
        self.last_used = time.time()
        if ok:
            self.success_count += 1
        else:
            self.failure_count += 1


class CapabilityRegistry:
    """Global registry of all discovered capabilities across sources."""

    def __init__(self):
        self._by_origin: dict[str, list[Capability]] = {}
        self._all: list[Capability] = []

    def register(self, cap: Capability) -> None:
        self._by_origin.setdefault(cap.origin, []).append(cap)
        self._all.append(cap)

    def unregister(self, origin: str, name: str) -> None:
        caps = self._by_origin.get(origin, [])
        self._by_origin[origin] = [c for c in caps if c.name != name]
        self._all = [c for c in self._all if not (c.origin == origin and c.name == name)]

    def get_for_origin(self, origin: str) -> list[Capability]:
        return self._by_origin.get(origin, [])

    def get_by_name(self, origin: str, name: str) -> Capability | None:
        for c in self._by_origin.get(origin, []):
            if c.name == name:
                return c
        return None

    def all(self) -> list[Capability]:
        return list(self._all)

    def find_capabilities(self, goal: str, origin: str | None = None,
                          max_risk: str = "critical") -> list[Capability]:
        """Find capabilities matching a goal, filtered by origin and max risk."""
        g = (goal or "").lower()
        candidates = self._all
        if origin:
            candidates = [c for c in candidates if c.origin == origin]
        max_r = RISK_ORDER.get(max_risk, 99)
        results = []
        for c in candidates:
            if RISK_ORDER.get(c.risk, 99) > max_r:
                continue
            # simple heuristic match on name/description
            if any(w in c.name.lower() or w in c.description.lower() for w in g.split()):
                results.append(c)
        results.sort()
        return results

    def choose_best(self, goal: str, origin: str | None = None,
                    available_sources: list[str] | None = None) -> Capability | None:
        """Return the single best capability for a goal (lowest risk, fastest)."""
        caps = self.find_capabilities(goal, origin)
        if available_sources:
            caps = [c for c in caps if c.source in available_sources]
        return caps[0] if caps else None


# Global singleton
REGISTRY = CapabilityRegistry()
