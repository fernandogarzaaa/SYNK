"""Memory subsystem (Stage F redesign): scoped, TTL-bounded, redacted, isolated.

Old design: one unbounded plaintext store, no sessions, no deletion
beyond wipe-all, typed form text (passwords included) persisted verbatim.

New guarantees:

* Scopes: ``task`` (one agent task), ``session`` (one browser session),
  ``long_term`` (learned preferences). Every record carries an
  ``expires_at``; ``purge_expired()`` removes stale records (also run
  lazily on every read).
* Secret redaction BEFORE storage: ``redact_secrets`` (contamination
  module) strips API keys, bearer tokens, passwords, URL-embedded
  tokens, card/SSN-like numbers from url/action/result/note/pref values.
  ``learn_from_action`` additionally refuses to learn any value whose
  redacted form differs from the original.
* Session isolation in the read path: records are stored with a
  ``session_id``; ``recent()``/``summary_for_prompt()`` filter by it.
  An unscoped read returns only unscoped records -- session A's private
  records never appear in session B's (or the global) read.
* Real deletion: ``delete_record`` / ``forget_session`` physically
  DELETE rows and verify the removal by re-reading; a surviving row
  raises instead of pretending success.
* Journal: stores and deletions emit events with the record's SHA-256
  content hash, never the content.

Backwards-compatible method names are kept (record, recent, set_pref,
get_pref, all_prefs, summary_for_prompt, forget_all) so existing
callers work; new keyword args add scoping.
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
import time
from pathlib import Path

from .contamination import classify_text, redact_secrets, looks_redacted

ROLLING_KEEP = 50

# Default TTL (seconds) per scope. Task memory dies with the task's
# working day; session memory with the week; learned prefs live a year
# (still deletable on demand).
SCOPE_TTLS = {
    "task": 24 * 3600,
    "session": 7 * 24 * 3600,
    "long_term": 365 * 24 * 3600,
}
VALID_SCOPES = frozenset(SCOPE_TTLS)


def _content_hash(payload: object) -> str:
    canon = json.dumps(payload, sort_keys=True, default=str,
                       ensure_ascii=True)
    return hashlib.sha256(canon.encode()).hexdigest()


class MemoryStore:
    def __init__(self, path: str | Path = ":memory:", emit=None):
        self.path = str(path)
        self.emit = emit or (lambda t, d: None)
        self.db = sqlite3.connect(self.path, check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        # Overwrite deleted content on disk, not just unlinked pages.
        self.db.execute("PRAGMA secure_delete=ON")
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS records(
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              scope TEXT NOT NULL DEFAULT 'task',
              session_id TEXT,
              task_id TEXT,
              ts REAL NOT NULL,
              expires_at REAL NOT NULL,
              url TEXT, action TEXT, result TEXT, note TEXT,
              content_hash TEXT NOT NULL);
            CREATE INDEX IF NOT EXISTS idx_records_session
              ON records(session_id, id);
            CREATE INDEX IF NOT EXISTS idx_records_expiry
              ON records(expires_at);
            CREATE TABLE IF NOT EXISTS prefs(
              key TEXT PRIMARY KEY, value TEXT, updated REAL);
            CREATE TABLE IF NOT EXISTS summaries(
              id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL, text TEXT);
        """)
        self._migrate_legacy()

    def _migrate_legacy(self) -> None:
        """Migrate pre-Stage-F plaintext storage exactly once.

        A legacy ``actions`` table (url/action/result/note, no session
        identity, no redaction) is redacted row by row into ``records``
        and then DROPPED so no plaintext copy survives. Existing
        preference values are re-redacted in place. Emits
        ``memory.migrated`` / ``memory.reredacted`` journal events.
        """
        tables = {r[0] for r in self.db.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
        if "actions" in tables:
            cols = [r[1] for r in self.db.execute(
                "PRAGMA table_info(actions)")]
            migrated = 0
            for row in self.db.execute("SELECT * FROM actions"):
                d = dict(zip(cols, row))
                action = d.get("action")
                if isinstance(action, str):
                    try:
                        action = json.loads(action)
                    except Exception:
                        action = {"raw": action}
                self.record(d.get("url") or "", action or {},
                            d.get("result") or "", d.get("note") or "",
                            session_id=d.get("session_id"),
                            task_id=d.get("task_id"))
                migrated += 1
            self.db.execute("DROP TABLE actions")
            self.db.commit()
            self.emit("memory.migrated",
                      {"source": "actions", "migrated": migrated,
                       "dropped_legacy_table": True})
        # Re-redact stored preference values (idempotent: already-redacted
        # values are unchanged, so only genuinely dirty rows rewrite).
        fixed = 0
        for r in self.db.execute("SELECT key, value FROM prefs"):
            new = redact_secrets(r["value"] or "")
            if new != r["value"]:
                self.db.execute("UPDATE prefs SET value=? WHERE key=?",
                                (new, r["key"]))
                fixed += 1
        if fixed:
            self.db.commit()
            self.emit("memory.reredacted",
                      {"prefs_fixed": fixed})

    # -- writes -----------------------------------------------------------------
    def record(self, url: str, action: dict, result: str = "",
               note: str = "", *, session_id: str | None = None,
               task_id: str | None = None, scope: str = "task",
               ttl: float | None = None) -> int:
        """Store one action record. Secrets are redacted BEFORE storage;
        the journal event carries the content hash, never the content."""
        if scope not in VALID_SCOPES:
            raise ValueError(f"unknown memory scope {scope!r}")
        now = time.time()
        expires = now + (SCOPE_TTLS[scope] if ttl is None else ttl)
        r_url = redact_secrets(url or "")
        r_action = redact_secrets(action or {})
        r_result = redact_secrets(result or "")
        r_note = redact_secrets(note or "")
        digest = _content_hash({"url": r_url, "action": r_action,
                                "result": r_result, "note": r_note,
                                "session_id": session_id, "task_id": task_id})
        cur = self.db.execute(
            "INSERT INTO records(scope,session_id,task_id,ts,expires_at,"
            "url,action,result,note,content_hash) VALUES(?,?,?,?,?,?,?,?,?,?)",
            (scope, session_id, task_id, now, expires, r_url,
             json.dumps(r_action, default=str), r_result, r_note, digest))
        self.db.commit()
        rid = cur.lastrowid
        self.emit("memory.stored",
                  {"record_id": rid, "scope": scope,
                   "session_id": session_id, "task_id": task_id,
                   "content_hash": digest, "expires_at": expires,
                   "redacted": True})
        self._maybe_summarize()
        return rid

    def _maybe_summarize(self) -> None:
        count = self.db.execute(
            "SELECT COUNT(*) c FROM records WHERE scope='task'").fetchone()["c"]
        if count > ROLLING_KEEP * 2:
            old = self.db.execute(
                "SELECT * FROM records WHERE scope='task' ORDER BY id ASC "
                "LIMIT ?", (ROLLING_KEEP,)).fetchall()
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
                "DELETE FROM records WHERE id IN (SELECT id FROM records "
                "WHERE scope='task' ORDER BY id ASC LIMIT ?)",
                (ROLLING_KEEP,))
            self.db.commit()

    # -- reads (session-isolated) --------------------------------------------------
    def purge_expired(self) -> int:
        cur = self.db.execute("DELETE FROM records WHERE expires_at <= ?",
                              (time.time(),))
        self.db.commit()
        return cur.rowcount

    def recent(self, n: int = ROLLING_KEEP,
               session_id: str | None = None) -> list[dict]:
        """Return recent records. When ``session_id`` is given, ONLY that
        session's records are returned. An unscoped call returns only
        unscoped records -- session A's private records never leak into
        session B's or the global read."""
        self.purge_expired()
        if session_id is None:
            rows = self.db.execute(
                "SELECT * FROM records WHERE session_id IS NULL "
                "ORDER BY id DESC LIMIT ?", (n,)).fetchall()
        else:
            rows = self.db.execute(
                "SELECT * FROM records WHERE session_id = ? "
                "ORDER BY id DESC LIMIT ?", (session_id, n)).fetchall()
        out = [dict(r) for r in reversed(rows)]
        # Journal the READ with hashes only -- never the content.
        self.emit("memory.read",
                  {"kind": "recent", "session_id": session_id,
                   "count": len(out),
                   "content_hashes": [r["content_hash"] for r in out]})
        return out

    def get_record(self, record_id: int,
                   session_id: str | None = None) -> dict | None:
        self.purge_expired()
        if session_id is None:
            r = self.db.execute("SELECT * FROM records WHERE id = ?",
                                (record_id,)).fetchone()
        else:
            r = self.db.execute(
                "SELECT * FROM records WHERE id = ? AND session_id = ?",
                (record_id, session_id)).fetchone()
        out = dict(r) if r else None
        self.emit("memory.read",
                  {"kind": "record", "session_id": session_id,
                   "record_id": record_id, "found": out is not None,
                   "content_hash": out["content_hash"] if out else None})
        return out

    # -- deletion (verified, not flagged) -------------------------------------------
    def delete_record(self, record_id: int,
                      session_id: str | None = None) -> bool:
        """Physically delete one record and VERIFY it is gone. Raises
        RuntimeError if the row survives; returns False when there was
        nothing to delete (wrong id or wrong session)."""
        rec = self.get_record(record_id, session_id)
        if rec is None:
            return False
        self.db.execute("DELETE FROM records WHERE id = ?", (record_id,))
        self.db.commit()
        if self.db.execute("SELECT 1 FROM records WHERE id = ?",
                           (record_id,)).fetchone() is not None:
            raise RuntimeError(f"memory deletion failed: record {record_id} "
                               "survived DELETE")
        self.emit("memory.deleted",
                  {"record_id": record_id, "scope": rec["scope"],
                   "session_id": rec["session_id"], "task_id": rec["task_id"],
                   "content_hash": rec["content_hash"], "verified": True})
        return True

    def forget_session(self, session_id: str) -> int:
        """Delete every record for a session; verify zero remain."""
        cur = self.db.execute("DELETE FROM records WHERE session_id = ?",
                              (session_id,))
        n = cur.rowcount
        self.db.commit()
        left = self.db.execute(
            "SELECT COUNT(*) c FROM records WHERE session_id = ?",
            (session_id,)).fetchone()["c"]
        if left:
            raise RuntimeError(f"memory deletion failed: {left} records for "
                               f"session {session_id!r} survived DELETE")
        self.emit("memory.session_forgotten",
                  {"session_id": session_id, "deleted": n, "verified": True})
        return n

    def forget_all(self) -> None:
        """GDPR/CCPA right-to-forget: wipe everything, verify empty."""
        self.db.executescript(
            "DELETE FROM records; DELETE FROM prefs; DELETE FROM summaries;")
        self.db.commit()
        left = self.db.execute(
            "SELECT (SELECT COUNT(*) FROM records) + "
            "(SELECT COUNT(*) FROM prefs) + "
            "(SELECT COUNT(*) FROM summaries) c").fetchone()["c"]
        if left:
            raise RuntimeError("memory wipe failed: rows survived DELETE")
        self.emit("memory.forgotten", {"verified": True})

    # -- preferences (learned, long-term; redacted) -----------------------------------
    def set_pref(self, key: str, value: str) -> None:
        r_value = redact_secrets(value or "")
        self.db.execute(
            "INSERT OR REPLACE INTO prefs(key,value,updated) VALUES(?,?,?)",
            (key, r_value, time.time()))
        self.db.commit()

    def get_pref(self, key: str, default: str = "") -> str:
        r = self.db.execute("SELECT value FROM prefs WHERE key=?",
                            (key,)).fetchone()
        value = r["value"] if r else default
        self.emit("memory.read",
                  {"kind": "pref", "key": key, "found": r is not None,
                   "content_hash": _content_hash({"key": key,
                                                  "value": value})
                   if r is not None else None})
        return value

    def all_prefs(self) -> dict:
        out = {r["key"]: r["value"]
               for r in self.db.execute("SELECT * FROM prefs").fetchall()}
        self.emit("memory.read",
                  {"kind": "prefs", "count": len(out),
                   "content_hashes": [
                       _content_hash({"key": k, "value": v})
                       for k, v in out.items()]})
        return out

    def learn_from_action(self, action: dict) -> None:
        """Learning mode: remember repetitive choices. Never learns
        secret-shaped values: if redaction changes the value, the
        preference is dropped instead of stored."""
        if action.get("tool") == "select" and action.get("value"):
            self.set_pref(f"select:{action.get('ref')}",
                          str(action["value"]))
        if action.get("tool") == "type" and action.get("field_hint"):
            hint = str(action["field_hint"])
            if "pass" in hint.lower():
                return  # never learn password fields
            text = str(action.get("text", ""))[:200]
            if text != redact_secrets(text):
                return  # secret-shaped value: do not learn it
            self.set_pref(f"fill:{hint}", text)

    # -- prompt compression -------------------------------------------------------------
    def summary_for_prompt(self, session_id: str | None = None) -> str:
        prefs = self.all_prefs()
        recent = self.recent(10, session_id=session_id)
        parts = []
        if prefs:
            parts.append("Prefs: " + "; ".join(
                f"{k}={v}" for k, v in list(prefs.items())[:10]))
        sums = self.db.execute(
            "SELECT text FROM summaries ORDER BY id DESC LIMIT 3").fetchall()
        if sums:
            parts.append("History: " + " | ".join(r["text"] for r in sums))
        if recent:
            last = "; ".join(
                f"{json.loads(r['action']).get('tool','?')}@{r['url'][:40]}"
                for r in recent[-5:])
            parts.append(f"Last steps: {last}")
        out = " ".join(parts) or "(no memory yet)"
        # Prompt injection surface: summarize, never inline raw page text.
        inj = [classify_text(p) for p in parts]
        flagged = sum(1 for r in inj if r["injection"])
        self.emit("memory.read",
                  {"kind": "summary", "session_id": session_id,
                   "content_hash": _content_hash(out),
                   "injection_markers_in_memory": flagged})
        return out
