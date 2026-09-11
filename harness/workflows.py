"""Workflow learning: learn workflow primitives, not raw history (Beta §9-11).

Pipeline:
  Human actions -> Event classifier -> Pattern detector -> Candidate routine
      -> Confirmation -> Workflow memory

Memory is structured (Beta §10):
  Preferences / Facts / Workflows / Site Adapters / Action Macros / Failure Patterns

Predictive preparation (§12): when a workflow matches (domain + intent,
no LLM needed), the harness pre-prepares sidebar state, memory, site context
and candidate tools so expensive reasoning starts from a prepared state.
Token usage drops *over time* as workflows are reused instead of rediscovered.
"""
from __future__ import annotations

import collections
import json
import sqlite3
import time
from pathlib import Path

MIN_RUNS_TO_CONFIRM = 3  # candidate -> confirmed workflow after N observations


def classify(action: dict) -> str:
    """Event classifier: raw action -> compact step token (deterministic)."""
    tool = action.get("tool", action.get("kind", "?"))
    target = action.get("target", action.get("selector", action.get("ref", "?")))
    domain = action.get("domain", "")
    return f"{domain}|{tool}@{target}".lower()


class WorkflowMiner:
    """Pattern detector over classified human-action sequences."""

    def __init__(self, min_len: int = 3, max_len: int = 8):
        self.min_len = min_len
        self.max_len = max_len
        self.sequences: list[list[str]] = []

    def observe(self, steps: list[str]) -> None:
        if len(steps) >= self.min_len:
            self.sequences.append(list(steps))

    def candidates(self) -> list[dict]:
        """Find repeated subsequences across observed runs."""
        counts: collections.Counter = collections.Counter()
        for seq in self.sequences:
            seen = set()
            for ln in range(self.min_len, min(self.max_len, len(seq)) + 1):
                for i in range(len(seq) - ln + 1):
                    gram = tuple(seq[i:i + ln])
                    if gram not in seen:
                        seen.add(gram)
                        counts[gram] += 1
        out = []
        for gram, n in counts.most_common(10):
            if n >= 2:  # seen in 2+ runs -> candidate
                domains = {s.split("|")[0] for s in gram if "|" in s}
                out.append({"steps": list(gram), "observed_runs": n,
                            "domain": sorted(domains)[0] if domains else "",
                            "confidence": min(0.99, 0.5 + 0.1 * n)})
        return out


class WorkflowMemory:
    """Structured memory: prefs / facts / workflows / adapters / macros / failures."""

    def __init__(self, path: str | Path = ":memory:"):
        self.db = sqlite3.connect(str(path), check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS prefs(key TEXT PRIMARY KEY, value TEXT);
            CREATE TABLE IF NOT EXISTS facts(key TEXT PRIMARY KEY, value TEXT);
            CREATE TABLE IF NOT EXISTS workflows(
              name TEXT PRIMARY KEY, domain TEXT, intent TEXT,
              steps TEXT, confidence REAL, observed_runs INTEGER, updated REAL);
            CREATE TABLE IF NOT EXISTS macros(name TEXT PRIMARY KEY, actions TEXT);
            CREATE TABLE IF NOT EXISTS failures(signature TEXT PRIMARY KEY,
              count INTEGER, last_seen REAL);
        """)

    # -- generic kv -----------------------------------------------------------
    def set(self, table: str, key: str, value: str) -> None:
        assert table in ("prefs", "facts")
        self.db.execute(f"INSERT OR REPLACE INTO {table}(key,value) VALUES(?,?)",
                        (key, value))
        self.db.commit()

    # -- workflows --------------------------------------------------------------
    def confirm(self, name: str, domain: str, intent: str,
                steps: list[str], confidence: float, runs: int) -> None:
        self.db.execute(
            "INSERT OR REPLACE INTO workflows(name,domain,intent,steps,confidence,"
            "observed_runs,updated) VALUES(?,?,?,?,?,?,?)",
            (name, domain, intent, json.dumps(steps), confidence, runs, time.time()))
        self.db.commit()

    def bump(self, name: str) -> None:
        r = self.db.execute("SELECT * FROM workflows WHERE name=?", (name,)).fetchone()
        if r:
            runs = r["observed_runs"] + 1
            conf = min(0.99, r["confidence"] + 0.02)
            self.db.execute("UPDATE workflows SET observed_runs=?, confidence=?, "
                            "updated=? WHERE name=?", (runs, conf, time.time(), name))
            self.db.commit()

    def match(self, domain: str, intent_hint: str = "") -> dict | None:
        """Predictive match: domain (+ optional intent), no LLM call."""
        rows = self.db.execute(
            "SELECT * FROM workflows WHERE domain=? ORDER BY confidence DESC",
            (domain,)).fetchall()
        if not rows:
            return None
        if intent_hint:
            for r in rows:
                if intent_hint.lower() in (r["intent"] or "").lower():
                    return dict(r)
        return dict(rows[0])

    def prepare(self, domain: str, intent_hint: str = "") -> dict:
        """Predictive preparation (§12): ready-to-use context without main LLM."""
        m = self.match(domain, intent_hint)
        if not m:
            return {"matched": False}
        return {"matched": True, "workflow": m["name"],
                "intent": m["intent"], "steps": json.loads(m["steps"]),
                "confidence": m["confidence"],
                "prepared": ["sidebar state", "relevant memory", "site context",
                             "candidate tools"],
                "suggestion": (f"You're doing '{m['intent']}' "
                                f"(seen {m['observed_runs']}x, "
                                f"conf {m['confidence']:.2f}). "
                                f"I can handle the repetitive portion while you continue.")}

    # -- failures ------------------------------------------------------------------
    def record_failure(self, signature: str) -> int:
        r = self.db.execute("SELECT count FROM failures WHERE signature=?",
                            (signature,)).fetchone()
        n = (r["count"] if r else 0) + 1
        self.db.execute("INSERT OR REPLACE INTO failures(signature,count,last_seen)"
                        " VALUES(?,?,?)", (signature, n, time.time()))
        self.db.commit()
        return n

    def all_workflows(self) -> list[dict]:
        return [dict(r) for r in
                self.db.execute("SELECT * FROM workflows ORDER BY confidence DESC")]
