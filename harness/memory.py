"""Memory subsystem: rolling log (last 50 verbatim) + summarized older history (spec 4).

Backed by SQLite (stdlib). Stores per-action memory notes, user preferences,
and a compressed summary that goes into every LLM prompt.
"""
from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path

ROLLING_KEEP = 50


class MemoryStore:
    def __init__(self, path: str | Path = ":memory:"):
        self.path = str(path)
        self.db = sqlite3.connect(self.path, check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS actions(
              id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL,
              url TEXT, action TEXT, result TEXT, note TEXT);
            CREATE TABLE IF NOT EXISTS prefs(
              key TEXT PRIMARY KEY, value TEXT, updated REAL);
            CREATE TABLE IF NOT EXISTS summaries(
              id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL, text TEXT);
        """)

    # -- actions -------------------------------------------------------------
    def record(self, url: str, action: dict, result: str = "", note: str = "") -> int:
        cur = self.db.execute(
            "INSERT INTO actions(ts,url,action,result,note) VALUES(?,?,?,?,?)",
            (time.time(), url, json.dumps(action), result, note))
        self.db.commit()
        self._maybe_summarize()
        return cur.lastrowid

    def recent(self, n: int = ROLLING_KEEP) -> list[dict]:
        rows = self.db.execute(
            "SELECT * FROM actions ORDER BY id DESC LIMIT ?", (n,)).fetchall()
        return [dict(r) for r in reversed(rows)]

    def _maybe_summarize(self) -> None:
        count = self.db.execute("SELECT COUNT(*) c FROM actions").fetchone()["c"]
        if count > ROLLING_KEEP * 2:
            # Summarize oldest batch into one line, then delete them (keep prefs intact).
            old = self.db.execute(
                "SELECT * FROM actions ORDER BY id ASC LIMIT ?", (ROLLING_KEEP,)).fetchall()
            urls = {r["url"] for r in old}
            tools = {}
            for r in old:
                try:
                    t = json.loads(r["action"]).get("tool", "?")
                except Exception:
                    t = "?"
                tools[t] = tools.get(t, 0) + 1
            text = (f"Earlier: {len(old)} steps across {len(urls)} pages; "
                    f"tools used {tools}.")
            self.db.execute("INSERT INTO summaries(ts,text) VALUES(?,?)",
                            (time.time(), text))
            self.db.execute(
                "DELETE FROM actions WHERE id IN (SELECT id FROM actions "
                "ORDER BY id ASC LIMIT ?)", (ROLLING_KEEP,))
            self.db.commit()

    # -- preferences (learned user behavior, spec 6) --------------------------
    def set_pref(self, key: str, value: str) -> None:
        self.db.execute(
            "INSERT OR REPLACE INTO prefs(key,value,updated) VALUES(?,?,?)",
            (key, value, time.time()))
        self.db.commit()

    def get_pref(self, key: str, default: str = "") -> str:
        r = self.db.execute("SELECT value FROM prefs WHERE key=?", (key,)).fetchone()
        return r["value"] if r else default

    def all_prefs(self) -> dict:
        return {r["key"]: r["value"]
                for r in self.db.execute("SELECT * FROM prefs").fetchall()}

    def learn_from_action(self, action: dict) -> None:
        """Learning mode: remember repetitive choices (e.g. always USPS)."""
        if action.get("tool") == "select" and action.get("value"):
            self.set_pref(f"select:{action.get('ref')}", str(action["value"]))
        if action.get("tool") == "type" and action.get("field_hint"):
            # only non-sensitive hints
            if "pass" not in str(action["field_hint"]).lower():
                self.set_pref(f"fill:{action['field_hint']}", str(action.get("text", ""))[:200])

    # -- prompt compression ----------------------------------------------------
    def summary_for_prompt(self) -> str:
        prefs = self.all_prefs()
        recent = self.recent(10)
        parts = []
        if prefs:
            parts.append("Prefs: " + "; ".join(f"{k}={v}" for k, v in list(prefs.items())[:10]))
        sums = self.db.execute(
            "SELECT text FROM summaries ORDER BY id DESC LIMIT 3").fetchall()
        if sums:
            parts.append("History: " + " | ".join(r["text"] for r in sums))
        if recent:
            last = "; ".join(
                f"{json.loads(r['action']).get('tool','?')}@{r['url'][:40]}"
                for r in recent[-5:])
            parts.append(f"Last steps: {last}")
        return " ".join(parts) or "(no memory yet)"

    def forget_all(self) -> None:
        """GDPR/CCPA right-to-forget (spec 10)."""
        self.db.executescript("DELETE FROM actions; DELETE FROM prefs; DELETE FROM summaries;")
        self.db.commit()
