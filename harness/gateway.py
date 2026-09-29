"""ExecutionGateway: the ONE canonical execution path (Stage C / Phase 10).

Every execution entrypoint -- /act, /transact, the Tauri shell's
submit_action (-> /act), the extension's runTask (-> /transact), the
closed-loop agent (/agent/*), benchmarks, and tests -- funnels through
``ExecutionGateway.execute()``. No frontend or caller may bypass the
transaction / ownership / verification pipeline; the HTTP endpoints are
thin adapters that only shape requests and responses.

The gateway runs every action through the TransactionEngine (strict mode):
per-action lifecycle, exclusive leases, fail-closed refs, per-action
claims through the single global Verifier, and an aggregate
TransactionVerification (COMMITTED / PARTIALLY_COMMITTED / FAILED /
UNVERIFIED). ROLLED_BACK is reported only if compensating rollback
handlers actually run; browser DOM mutations are not ACID, so this is
documented as "transactional orchestration with explicit partial-commit
semantics".
"""
from __future__ import annotations

import json
import time
from typing import Any, Callable

from .session import new_id
from .transactions import TransactionEngine


class ExecutionGateway:
    """Canonical gateway over a TransactionEngine plus runtime services."""

    def __init__(self, engine: TransactionEngine, *,
                 mem=None,
                 emit: Callable[[str, dict], Any] | None = None):
        self.engine = engine
        self.mem = mem            # MemoryStore (may be None in tests)
        self.emit = emit or (lambda t, d: None)

    def execute(self, request: dict) -> dict:
        """Execute one canonical execution request.

        Request fields:
          actions | action : action dict(s) to run
          task_id          : caller-supplied or minted
          transaction_id   : informational only (engine mints per run)
          tab_id, session_id, window_id, frame_id : execution identity
          page_url         : page the actions were planned against
          user_consented   : consent flag for destructive actions
          note             : memory note

        Returns a dict with per-action results, per-action verifications,
        and the aggregate transaction status. HTTP adapters shape this
        into endpoint responses; the content is identical for all callers.
        """
        t0 = time.time()
        actions = request.get("actions")
        if actions is None:
            single = request.get("action")
            actions = [single] if single else []
        tab_id = request.get("tab_id", "default")
        task_id = request.get("task_id") or new_id("t")
        page_url = request.get("page_url", "")
        consented = bool(request.get("user_consented", False))

        # Identity is captured on each action; execution targets the tab the
        # task pinned, never the browser's "current tab".
        for a in actions:
            if isinstance(a, dict):
                a.setdefault("tab_id", tab_id)
                if request.get("session_id"):
                    a.setdefault("session_id", request["session_id"])
                if request.get("window_id"):
                    a.setdefault("window_id", request["window_id"])
                if request.get("frame_id"):
                    a.setdefault("frame_id", request["frame_id"])
                a.setdefault("task_id", task_id)

        report = self.engine.execute(
            actions, task_id=task_id, tab_id=tab_id,
            session_id=request.get("session_id"),
            window_id=request.get("window_id", "win_default"),
            frame_id=request.get("frame_id", "main"),
            page_url=page_url, user_consented=consented, strict=True)

        # Memory learning + recording (moved here from the endpoint
        # handlers so every caller learns identically). Stage F: records
        # are task-scoped and session-isolated; secrets are redacted
        # before storage and the journal gets a content hash, not content.
        if self.mem is not None:
            note = request.get("note", "")
            for a, ex in zip(actions, report.executions):
                if isinstance(a, dict):
                    try:
                        self.mem.learn_from_action(a)
                        self.mem.record(page_url, a,
                                        json.dumps(ex.to_dict())[:500], note,
                                        session_id=request.get("session_id"),
                                        task_id=task_id, scope="task")
                    except Exception:
                        pass

        return {"ok": True,
                "task_id": task_id,
                "transaction_id": report.transaction_id,
                "transaction_status": report.status,
                "action_results": [e.to_dict()
                                   for e in report.executions],
                "verifications": [e.verification
                                  for e in report.executions],
                "latency_ms": round((time.time() - t0) * 1000, 2)}
