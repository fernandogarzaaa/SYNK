"""Stage G model router (Phase 14): honest capability-based model selection.

The router selects among CONFIGURED model backends by declared capability,
not by vibes. The registry comes from the operator:

  - SYNK_MODELS: a JSON list of backend entries, or
  - SYNK_MODELS_FILE: path to a JSON file with the same shape.

Entry shape:
  {"name": "local-qwen3-8b", "provider": "ollama",
   "capabilities": ["text", "reasoning", "tool_use"],
   "context_tokens": 32768, "cost_tier": "free",
   "notes": "operator description"}

Recognized capabilities: text, vision, reasoning, tool_use, long_context.
Recognized cost tiers (operator-declared, NOT benchmarks): free, cheap,
expensive.

Task requirements: {"needs_vision": bool, "long_horizon": bool,
"needs_reasoning": bool}.

Selection is deterministic:
  1. eliminate candidates missing a required capability,
  2. among survivors, prefer larger declared context when long_horizon,
  3. then cheaper cost tier,
  4. ties break by name.

If only one backend is configured, the router says so and routes to it.
No benchmark numbers are invented anywhere in this module.
"""
from __future__ import annotations

import json
import os
from pathlib import Path

CAPABILITIES = ("text", "vision", "reasoning", "tool_use", "long_context")
COST_ORDER = {"free": 0, "cheap": 1, "expensive": 2}

_DEFAULT_ENTRY = {
    "name": "local:deterministic",
    "provider": "built-in",
    "capabilities": ["text"],
    "context_tokens": None,
    "cost_tier": "free",
    "notes": ("No SYNK_MODELS / SYNK_MODELS_FILE configured; single "
              "built-in default backend. Configure a registry to route "
              "among real backends."),
}


class ModelRegistry:
    """The operator-declared set of model backends."""

    def __init__(self, entries: list[dict], *, source: str):
        self.source = source
        self.entries: list[dict] = []
        for e in entries:
            self.entries.append(self._normalize(e))
        if not self.entries:
            raise ValueError("model registry must declare at least one "
                             "backend")

    @staticmethod
    def _normalize(e: dict) -> dict:
        if not isinstance(e, dict) or not e.get("name"):
            raise ValueError(f"invalid registry entry: {e!r}")
        caps = [c for c in (e.get("capabilities") or ["text"])
                if c in CAPABILITIES]
        if not caps:
            caps = ["text"]
        cost = e.get("cost_tier", "free")
        if cost not in COST_ORDER:
            cost = "free"
        return {"name": str(e["name"]),
                "provider": str(e.get("provider", "unknown")),
                "capabilities": caps,
                "context_tokens": e.get("context_tokens"),
                "cost_tier": cost,
                "notes": str(e.get("notes", ""))}

    @classmethod
    def from_env(cls) -> "ModelRegistry":
        raw = os.environ.get("SYNK_MODELS")
        if raw:
            try:
                entries = json.loads(raw)
            except json.JSONDecodeError as e:
                raise ValueError(f"SYNK_MODELS is not valid JSON: {e}")
            return cls(entries, source="env: SYNK_MODELS")
        path = os.environ.get("SYNK_MODELS_FILE")
        if path:
            p = Path(path).expanduser()
            if not p.is_file():
                raise ValueError(f"SYNK_MODELS_FILE not found: {path}")
            try:
                entries = json.loads(p.read_text())
            except json.JSONDecodeError as e:
                raise ValueError(f"{path} is not valid JSON: {e}")
            return cls(entries, source=f"file: {p}")
        return cls([dict(_DEFAULT_ENTRY)], source="built-in default")

    def to_dict(self) -> dict:
        return {"source": self.source, "backends": list(self.entries),
                "count": len(self.entries)}


class RoutingDecision(dict):
    """A recorded routing decision (a plain dict with fixed keys)."""


class ModelRouter:
    """Deterministic capability-based routing over a ModelRegistry."""

    def __init__(self, registry: ModelRegistry):
        self.registry = registry

    def route(self, requirements: dict | None = None, *,
              task_id: str | None = None) -> RoutingDecision:
        req = dict(requirements or {})
        need_vision = bool(req.get("needs_vision", False))
        long_horizon = bool(req.get("long_horizon", False))
        need_reasoning = bool(req.get("needs_reasoning", False))
        required = {"text"}
        if need_vision:
            required.add("vision")
        if long_horizon:
            required.add("long_context")
        if need_reasoning:
            required.add("reasoning")

        scored = []
        for e in self.registry.entries:
            missing = sorted(required - set(e["capabilities"]))
            ctx = e.get("context_tokens")
            scored.append({
                "name": e["name"],
                "missing": missing,
                "context_tokens": ctx,
                "cost_tier": e["cost_tier"],
                "eligible": not missing,
            })
        eligible = [s for s in scored if s["eligible"]]

        rationale = []
        if len(self.registry.entries) == 1:
            rationale.append("single backend configured; routing to it "
                             "(no choice to make)")
        rationale.append(
            "required capabilities: " + ", ".join(sorted(required)))

        if not eligible:
            # Fail closed: no backend can do the job. Say so, with the
            # missing capabilities named, instead of picking a bad one.
            rationale.append(
                "no configured backend satisfies the requirements; "
                "routing refused")
            return RoutingDecision({
                "ok": False, "task_id": task_id,
                "selected": None, "candidates": scored,
                "rationale": rationale,
                "registry_source": self.registry.source,
                "single_backend": len(self.registry.entries) == 1,
            })

        def rank(s: dict):
            ctx = s["context_tokens"]
            ctx_rank = -(ctx if isinstance(ctx, (int, float)) else 0) \
                if long_horizon else 0
            return (ctx_rank, COST_ORDER.get(s["cost_tier"], 0), s["name"])

        eligible.sort(key=rank)
        winner = eligible[0]
        rationale.append(
            f"selected '{winner['name']}': covers all required "
            f"capabilities"
            + (f", largest declared context "
               f"({winner['context_tokens']} tokens)" if long_horizon
               and isinstance(winner["context_tokens"], (int, float))
               else "")
            + f", cost tier '{winner['cost_tier']}'")
        if len(eligible) > 1:
            rationale.append(
                "rejected: " + ", ".join(
                    f"{s['name']} (cost {s['cost_tier']})"
                    for s in eligible[1:]))
        return RoutingDecision({
            "ok": True, "task_id": task_id,
            "selected": winner["name"], "candidates": scored,
            "rationale": rationale,
            "registry_source": self.registry.source,
            "single_backend": len(self.registry.entries) == 1,
        })
