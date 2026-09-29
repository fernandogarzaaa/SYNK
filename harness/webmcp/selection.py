"""Deterministic WebMCP capability selection (Stage E).

Given a task goal and the tools a page actually advertised, rank the
candidates and pick one -- with a written record of every candidate
considered, its score breakdown, and why the winner was chosen. The
record goes into the transaction's audit trail (agent-loop context notes
and the ``webmcp.selected`` bus event), so selection is reviewable, not
an opaque pick.

Scoring (fully deterministic; ties cannot happen silently):

* The goal is tokenized into lowercase alphanumeric words.
* A tool name is tokenized by splitting camelCase, snake_case and
  kebab-case (``searchProducts`` -> {search, products}).
* ``name_hits``   = goal words intersecting name tokens.
* ``desc_hits``   = goal words intersecting description words.
* ``schema_bonus`` = +2 when every *required* input parameter name
  appears as a substring of the goal (the tool is directly applicable).
* ``score = 3 * name_hits + 1 * desc_hits + schema_bonus``.
* Ordering: score descending, then risk ascending
  (low < medium < high < critical), then tool name ascending.

Risk uses the same mapping as the capability registry: a destructive
tool is critical, a read-only tool is low, anything else is medium.
``max_risk`` filters candidates before scoring (default: critical, i.e.
no filter); the audit record lists filtered-out tools separately so a
dropped candidate is visible, not silent.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

_WORD = re.compile(r"[a-z0-9]+")


def _words(text: str) -> set[str]:
    return set(_WORD.findall((text or "").lower()))


def _name_tokens(name: str) -> set[str]:
    # camelCase -> "camel case", then word-split; also splits _ and -.
    spaced = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", " ", name or "")
    spaced = spaced.replace("_", " ").replace("-", " ")
    return _words(spaced)


def risk_for_tool(tool: dict) -> str:
    ann = (tool or {}).get("annotations") or {}
    if ann.get("destructiveHint"):
        return "critical"
    if ann.get("readOnlyHint"):
        return "low"
    return "medium"


_RISK_ORDER = {"low": 0, "medium": 1, "high": 2, "critical": 3}


@dataclass
class CandidateScore:
    name: str
    score: int
    name_hits: list[str]
    desc_hits: list[str]
    schema_bonus: int
    risk: str
    description: str = ""

    def to_dict(self) -> dict:
        return {"name": self.name, "score": self.score,
                "breakdown": {"name_hits": self.name_hits,
                              "desc_hits": self.desc_hits,
                              "schema_bonus": self.schema_bonus},
                "risk": self.risk, "description": self.description}


@dataclass
class SelectionRecord:
    """The auditable outcome of one selection round."""
    goal: str
    scope_key: str
    candidates: list[CandidateScore] = field(default_factory=list)
    filtered_out: list[dict] = field(default_factory=list)
    winner: str | None = None
    rationale: str = ""

    @property
    def considered(self) -> int:
        return len(self.candidates) + len(self.filtered_out)

    def to_dict(self) -> dict:
        return {"goal": self.goal, "scope_key": self.scope_key,
                "considered": self.considered,
                "candidates": [c.to_dict() for c in self.candidates],
                "filtered_out": self.filtered_out,
                "winner": self.winner, "rationale": self.rationale}


class CapabilitySelector:
    """Ranks page-advertised WebMCP tools against a goal."""

    def select(self, goal: str, tools: list[dict],
               scope_key: str = "",
               max_risk: str = "critical") -> SelectionRecord:
        tools = tools or []
        goal_words = _words(goal)
        rec = SelectionRecord(goal=goal or "", scope_key=scope_key)
        ceiling = _RISK_ORDER.get(max_risk, 99)
        scored: list[CandidateScore] = []
        for t in tools:
            name = str(t.get("name", ""))
            risk = risk_for_tool(t)
            if _RISK_ORDER.get(risk, 99) > ceiling:
                rec.filtered_out.append(
                    {"name": name, "risk": risk,
                     "reason": f"risk {risk} exceeds max_risk {max_risk}"})
                continue
            name_toks = _name_tokens(name)
            desc_toks = _words(t.get("description", ""))
            name_hits = sorted(goal_words & name_toks)
            desc_hits = sorted(goal_words & desc_toks)
            required = ((t.get("input_schema") or {}).get("required")) or []
            schema_bonus = 0
            if required and all(str(p).lower() in (goal or "").lower()
                                for p in required):
                schema_bonus = 2
            score = 3 * len(name_hits) + len(desc_hits) + schema_bonus
            scored.append(CandidateScore(
                name=name, score=score, name_hits=name_hits,
                desc_hits=desc_hits, schema_bonus=schema_bonus, risk=risk,
                description=str(t.get("description", ""))[:200]))
        scored.sort(key=lambda c: (-c.score, _RISK_ORDER.get(c.risk, 99),
                                   c.name))
        rec.candidates = scored
        if scored and scored[0].score > 0:
            win = scored[0]
            rec.winner = win.name
            rec.rationale = (
                f"selected '{win.name}' (score={win.score}: "
                f"name_hits={win.name_hits} x3, desc_hits={win.desc_hits} x1, "
                f"schema_bonus={win.schema_bonus}; risk={win.risk}) over "
                f"{len(scored) - 1} other candidate(s); "
                f"{len(rec.filtered_out)} filtered by max_risk={max_risk}")
        elif scored:
            rec.rationale = (
                f"no candidate matched the goal (all scores 0); "
                f"{len(scored)} candidate(s) considered, none selected")
        else:
            rec.rationale = "no candidate tools advertised for this scope"
        return rec
