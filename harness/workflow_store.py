"""Stage G workflow learning (Phase 13): learn from VERIFIED work, not raw
history.

A workflow is a named, parameterized sequence of steps recorded ONLY from
transactions whose every action independently verified (VerificationResult
result == VERIFIED in the event journal's transaction reports). Failed or
unverified runs are never learned from.

  - record/learn: WorkflowLearner.learn_from_report(report, ...) extracts
    the verified action sequence, parameterizes the literal values
    (text/value/url -> {{p0}}, {{p1}}, ... with recorded defaults), and
    stores it with provenance (task_id, session_id, domain/origin,
    success counts). Re-observing the same normalized step shape bumps
    observed_runs/success_count instead of duplicating.
  - suggest: deterministic scoring of a goal against stored workflows;
    every suggestion carries a recorded rationale (which terms matched,
    provenance, confidence). No LLM, no hidden weights.
  - replay: render params into steps and execute through the SAME
    transaction engine (caller-supplied executor, e.g. the execution
    gateway) with per-action verification. A replay is never blind: the
    returned result is the engine's real report.

Storage is sqlite (stdlib). Workflow names are canonical:
wf_<sha256(normalized steps)[:12]>.
"""
from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import time
from pathlib import Path

from .verification.results import VERIFIED

# action keys whose literal values become workflow parameters
_PARAM_KEYS = ("text", "value", "url", "query", "path", "key")

_TOKEN_RE = re.compile(r"[a-z0-9]+")


def _canonical(payload: object) -> str:
    return json.dumps(payload, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=True, default=str)


def _workflow_key(normalized_steps: list[dict]) -> str:
    return "wf_" + hashlib.sha256(
        _canonical(normalized_steps).encode()).hexdigest()[:12]


def _tokenize(text: str) -> list[str]:
    return _TOKEN_RE.findall((text or "").lower())


def _parameterize(actions: list[dict]) -> tuple[list[dict], dict]:
    """Replace literal values with {{pN}} params; return (steps, defaults)."""
    steps: list[dict] = []
    defaults: dict = {}
    counter = 0
    for a in actions:
        step = dict(a)
        for k in _PARAM_KEYS:
            if k in step and isinstance(step[k], (str, int, float)) \
                    and step[k] not in (None, ""):
                pname = f"p{counter}"
                counter += 1
                defaults[pname] = step[k]
                step[k] = "{{%s}}" % pname
        steps.append(step)
    return steps, defaults


def _normalized_shape(steps: list[dict]) -> list[dict]:
    """Step shape with values blanked: what 'same workflow' means."""
    shape = []
    for s in steps:
        sh = {"tool": s.get("tool")}
        for k in ("ref", "target", "selector"):
            if s.get(k) not in (None, ""):
                sh[k] = s[k]
        shape.append(sh)
    return shape


class WorkflowLearner:
    """Record verified action sequences as reusable, parameterized workflows."""

    def __init__(self, path: str | Path = ":memory:"):
        self.db = sqlite3.connect(str(path), check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS workflows(
              name TEXT PRIMARY KEY, goal TEXT, domain TEXT, origin TEXT,
              steps TEXT, params TEXT, defaults TEXT,
              confidence REAL, observed_runs INTEGER, success_count INTEGER,
              provenance TEXT, created REAL, updated REAL);
            CREATE TABLE IF NOT EXISTS suggestions(
              id INTEGER PRIMARY KEY AUTOINCREMENT, goal TEXT,
              workflow_name TEXT, score REAL, rationale TEXT, ts REAL);
        """)

    # -- learning ---------------------------------------------------------------
    def learn_from_report(self, report, *, goal: str, domain: str = "",
                          origin: str = "", session_id: str | None = None,
                          task_id: str | None = None) -> dict | None:
        """Learn from a TransactionReport. Returns the workflow record, or
        None when nothing was learnable (no fully-verified action)."""
        executions = getattr(report, "executions", None) or []
        pairs: list[tuple[dict, dict]] = []
        for ex in executions:
            ver = getattr(ex, "verification", None) or {}
            action = getattr(getattr(ex, "request", None), "action", None)
            if isinstance(action, dict) and isinstance(ver, dict):
                pairs.append((action, ver))
        return self.learn_verified_pairs(
            pairs, goal=goal, domain=domain, origin=origin,
            session_id=session_id, task_id=task_id)

    def learn_verified_pairs(self, pairs: list[tuple[dict, dict]], *,
                             goal: str, domain: str = "",
                             origin: str = "", session_id: str | None = None,
                             task_id: str | None = None) -> dict | None:
        """Learn from (action, verification_dict) pairs. Only pairs whose
        verification result is VERIFIED contribute steps."""
        verified_actions = [a for a, ver in pairs
                            if ver.get("result") == VERIFIED]
        if not verified_actions:
            return {"ok": False, "learned": False,
                    "reason": "no VERIFIED actions in report"}
        steps, defaults = _parameterize(verified_actions)
        key = _workflow_key(_normalized_shape(steps))
        now = time.time()
        row = self.db.execute("SELECT * FROM workflows WHERE name=?",
                              (key,)).fetchone()
        params = sorted(defaults)
        if row is None:
            provenance = [{"task_id": task_id, "session_id": session_id,
                           "ts": now}]
            self.db.execute(
                "INSERT INTO workflows(name,goal,domain,origin,steps,params,"
                "defaults,confidence,observed_runs,success_count,"
                "provenance,created,updated) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (key, goal, domain, origin, _canonical(steps),
                 _canonical(params), _canonical(defaults), 0.6, 1, 1,
                 _canonical(provenance), now, now))
            self.db.commit()
        else:
            runs = row["observed_runs"] + 1
            succ = row["success_count"] + 1
            conf = min(0.99, 0.6 + 0.05 * (succ - 1))
            prov = json.loads(row["provenance"] or "[]")
            prov.append({"task_id": task_id, "session_id": session_id,
                         "ts": now})
            prov = prov[-20:]  # bounded provenance trail
            self.db.execute(
                "UPDATE workflows SET observed_runs=?, success_count=?, "
                "confidence=?, provenance=?, goal=?, updated=? WHERE name=?",
                (runs, succ, conf, _canonical(prov), goal or row["goal"],
                 now, key))
            self.db.commit()
        return {"ok": True, "learned": True, "name": key,
                "verified_steps": len(verified_actions)}

    # -- retrieval ---------------------------------------------------------------
    def list(self) -> list[dict]:
        rows = self.db.execute(
            "SELECT * FROM workflows ORDER BY success_count DESC, "
            "confidence DESC").fetchall()
        return [self._row_to_dict(r) for r in rows]

    def get(self, name: str) -> dict | None:
        row = self.db.execute("SELECT * FROM workflows WHERE name=?",
                              (name,)).fetchone()
        return self._row_to_dict(row) if row else None

    @staticmethod
    def _row_to_dict(r) -> dict:
        d = dict(r)
        for k in ("steps", "params", "defaults", "provenance"):
            try:
                d[k] = json.loads(d[k] or ("[]" if k != "defaults" else "{}"))
            except Exception:
                d[k] = [] if k != "defaults" else {}
        return d

    # -- suggestion ---------------------------------------------------------------
    def suggest(self, goal: str, *, top_n: int = 3) -> list[dict]:
        """Deterministic match of a goal against stored workflows.

        Score = 0.7 * (matched goal tokens / goal tokens) + 0.2 * confidence
        + 0.1 * min(success_count,10)/10. Ties break by name (deterministic).
        Every suggestion records its rationale in the suggestions table.
        """
        goal_tokens = _tokenize(goal)
        if not goal_tokens:
            return []
        goal_set = set(goal_tokens)
        scored = []
        for w in self.list():
            hay = " ".join([
                w.get("goal", ""), w.get("domain", ""),
                " ".join(str(s.get("tool", "")) for s in w["steps"]),
                " ".join(str(s.get("target", "")) for s in w["steps"]),
            ])
            hay_tokens = set(_tokenize(hay))
            matched = sorted(goal_set & hay_tokens)
            coverage = len(matched) / len(goal_set)
            score = round(0.7 * coverage + 0.2 * w["confidence"]
                          + 0.1 * min(w["success_count"], 10) / 10, 4)
            rationale = [
                f"matched {len(matched)}/{len(goal_set)} goal terms: "
                f"{', '.join(matched) if matched else 'none'}",
                f"provenance: {w['success_count']} verified success(es) "
                f"across {w['observed_runs']} run(s), "
                f"confidence {w['confidence']:.2f}",
                f"domain '{w['domain'] or 'any'}', "
                f"{len(w['steps'])} parameterized step(s)",
            ]
            scored.append((score, w["name"], w, rationale))
        scored.sort(key=lambda t: (-t[0], t[1]))
        out = []
        now = time.time()
        for score, _name, w, rationale in scored[:max(1, top_n)]:
            if score <= 0:
                continue
            self.db.execute(
                "INSERT INTO suggestions(goal,workflow_name,score,rationale,"
                "ts) VALUES(?,?,?,?,?)",
                (goal, w["name"], score, _canonical(rationale), now))
            out.append({"name": w["name"], "goal": w["goal"],
                        "domain": w["domain"], "score": score,
                        "rationale": rationale,
                        "steps": w["steps"], "params": w["params"],
                        "success_count": w["success_count"],
                        "confidence": w["confidence"]})
        self.db.commit()
        return out

    # -- replay --------------------------------------------------------------------
    def render(self, name: str, params: dict | None = None) -> list[dict]:
        """Render a workflow's steps with params substituted (defaults fill
        the rest). Raises KeyError for unknown workflows."""
        w = self.get(name)
        if w is None:
            raise KeyError(f"unknown workflow '{name}'")
        merged = dict(w["defaults"] or {})
        merged.update(params or {})
        rendered = []
        for step in w["steps"]:
            out = {}
            for k, v in step.items():
                if isinstance(v, str) and v.startswith("{{") \
                        and v.endswith("}}"):
                    pname = v[2:-2]
                    if pname not in merged:
                        raise KeyError(
                            f"workflow '{name}': missing param '{pname}' "
                            "and no default recorded")
                    out[k] = merged[pname]
                else:
                    out[k] = v
            rendered.append(out)
        return rendered

    def replay(self, name: str, params: dict | None, executor,
               **identity) -> dict:
        """Replay a workflow THROUGH the transaction engine.

        executor(actions: list[dict]) -> dict is the caller's engine entry
        (e.g. ExecutionGateway.execute bound with identity). The replay is
        never blind: the returned dict carries the engine's real report.
        """
        actions = self.render(name, params)
        result = executor(actions, **identity)
        if not isinstance(result, dict):
            result = {"ok": False, "error": "executor returned "
                                           f"{type(result).__name__}"}
        return {"ok": result.get("ok", False), "workflow": name,
                "rendered_actions": actions, "engine_result": result}
