"""Concurrency layer: shared-state co-execution with EXCLUSIVE leases.

Stage B: LeaseManager.acquire() now genuinely enforces exclusive agent
ownership. Two agents cannot hold the same exclusive resource at once;
release is compare-and-release (a stale lease can never release a newer
lease's target); all ownership transitions go through one synchronized
state machine; TTLs are bounded with automatic pruning.

Ownership hierarchy (session > tab > frame > resource > element) is
supported via ancestor target keys: acquiring a leaf also checks its
ancestors, so a human-owned tab blocks agent acquisition of elements
inside it.

A global emergency stop is separate from resource-level ownership: it
revokes all agent leases and blocks new acquisitions until cleared.
Human ownership is non-locking and decays (HUMAN_TTL).
"""
from __future__ import annotations

import threading
import time
import uuid

FREE, HUMAN_OWNED, AGENT_OWNED, CONFLICT = "FREE", "HUMAN_OWNED", "AGENT_OWNED", "CONFLICT"

HUMAN_TTL = 5.0   # human ownership decays; the human is not a lock either
MIN_LEASE_TTL = 0.1
MAX_LEASE_TTL = 30.0  # TTLs are bounded


def _clamp_ttl(ttl: float) -> float:
    try:
        ttl = float(ttl)
    except (TypeError, ValueError):
        ttl = 2.0
    return max(MIN_LEASE_TTL, min(MAX_LEASE_TTL, ttl))


class OwnershipGraph:
    """One synchronized ownership state machine.

    All mutations go through _transition(); callers cannot arbitrarily
    overwrite ownership records. Thread-safe via an RLock so acquire is
    atomic.
    """

    def __init__(self):
        self._lock = threading.RLock()
        # target -> {state, owner ("human"/"agent"/None), lease (lease_id|None),
        #            actor, until (epoch)}
        self.nodes: dict[str, dict] = {}

    # -- internal ------------------------------------------------------------
    def _get_locked(self, target: str) -> dict:
        n = self.nodes.get(target)
        if n is None:
            return {"state": FREE, "owner": None, "lease": None,
                    "actor": None, "until": 0.0}
        if n["state"] != FREE and n["until"] < time.time():
            return {"state": FREE, "owner": None, "lease": None,
                    "actor": None, "until": 0.0}
        return n

    def _transition(self, target: str, to_state: str, owner: str | None,
                    lease: str | None, actor: str | None,
                    ttl: float) -> dict:
        """The ONLY writer of ownership records."""
        rec = {"state": to_state, "owner": owner, "lease": lease,
               "actor": actor, "until": time.time() + ttl}
        self.nodes[target] = rec
        return rec

    # -- reads -----------------------------------------------------------------
    def state_of(self, target: str) -> str:
        with self._lock:
            return self._get_locked(target)["state"]

    def owner_of(self, target: str) -> dict:
        """Full ownership record (state, owner, lease, actor, until)."""
        with self._lock:
            return dict(self._get_locked(target))

    # -- transitions -------------------------------------------------------------
    def acquire_agent(self, target: str, lease_id: str, actor: str = "agent",
                      ttl: float = 2.0) -> bool:
        """Atomically take exclusive agent ownership.

        Succeeds only from FREE, or re-entrantly when the SAME lease already
        holds the target (renews TTL). HUMAN_OWNED / CONFLICT / AGENT_OWNED
        by another lease all fail.
        """
        ttl = _clamp_ttl(ttl)
        with self._lock:
            cur = self._get_locked(target)
            if cur["state"] == FREE:
                self._transition(target, AGENT_OWNED, "agent", lease_id,
                                 actor, ttl)
                return True
            if cur["state"] == AGENT_OWNED and cur["lease"] == lease_id:
                self._transition(target, AGENT_OWNED, "agent", lease_id,
                                 actor, ttl)
                return True
            return False

    def acquire_human(self, target: str, ttl: float = HUMAN_TTL) -> str:
        """Record human interaction. Non-locking; decays.

        Returns the resulting state. Touching an agent-owned target marks
        CONFLICT (human wins the race visibly); otherwise HUMAN_OWNED.
        """
        with self._lock:
            cur = self._get_locked(target)
            state = CONFLICT if cur["state"] == AGENT_OWNED else HUMAN_OWNED
            self._transition(target, state, "human", None, "human", ttl)
            return state

    def mark_human(self, target: str, ttl: float = HUMAN_TTL) -> str:
        """Compat alias for acquire_human (human activity is non-locking)."""
        return self.acquire_human(target, ttl)

    def release(self, target: str, lease_id: str | None) -> bool:
        """Compare-and-release: only the current lease holder can release.

        A stale lease id (or None) never releases a target owned by a newer
        lease. Returns True iff the release took effect.
        """
        with self._lock:
            cur = self._get_locked(target)
            if cur["state"] != AGENT_OWNED:
                # Releasing a free/human-owned target is a no-op success only
                # when there is genuinely nothing agent-held to protect.
                return cur["state"] == FREE
            if lease_id is not None and cur["lease"] != lease_id:
                return False  # stale lease: refuse
            self.nodes.pop(target, None)
            return True

    def prune(self) -> int:
        """Drop expired ownership records. Returns count pruned."""
        with self._lock:
            now = time.time()
            expired = [t for t, n in self.nodes.items()
                       if n["state"] != FREE and n["until"] < now]
            for t in expired:
                self.nodes.pop(t, None)
            return len(expired)

    def snapshot(self) -> dict:
        with self._lock:
            now = time.time()
            return {t: n["state"] for t, n in self.nodes.items()
                    if n["until"] >= now}

    def detailed_snapshot(self) -> dict:
        with self._lock:
            now = time.time()
            return {t: dict(n) for t, n in self.nodes.items()
                    if n["until"] >= now}


class LeaseManager:
    """Short-lived exclusive action leases: {actor, target, intent, lease, ttl}.

    acquire() REJECTS when the target (or any ancestor in the ownership
    hierarchy) is held by another lease, human-owned, or conflicted.
    release() is compare-and-release. Expired leases are pruned
    automatically. emergency_stop() revokes everything agent-held.
    """

    def __init__(self, ownership: OwnershipGraph):
        self.ownership = ownership
        self.leases: dict[str, dict] = {}  # lease_id -> record
        self._lock = threading.RLock()
        self._estop = False

    # -- emergency stop (global, separate from resource ownership) --------------
    def emergency_stop(self) -> int:
        """Revoke ALL agent leases and block new acquisitions. Returns count."""
        with self._lock:
            self._estop = True
            count = 0
            for lid, info in list(self.leases.items()):
                if self.ownership.release(info["target"], lid):
                    count += 1
                self.leases.pop(lid, None)
            return count

    def clear_emergency(self) -> None:
        with self._lock:
            self._estop = False

    @property
    def stopped(self) -> bool:
        return self._estop

    # -- acquire / release -------------------------------------------------------
    def acquire(self, target: str, intent: str = "", ttl: float = 2.0, *,
                actor: str = "agent", task_id: str | None = None,
                action_id: str | None = None,
                owner_hierarchy: tuple[str, ...] = (),
                nest_under_task_id: str | None = None) -> dict | None:
        """Atomically acquire an exclusive lease, or return None.

        owner_hierarchy lists ancestor target keys (e.g. ("tab:3",)) that must
        also be free of human ownership / conflicts / foreign agent leases.

        nest_under_task_id: when set, an ancestor held AGENT_OWNED by a lease
        whose record carries the SAME task_id is allowed (nested action
        lease under the task's own tab lease, e.g. the Stage G scheduler
        holds "tab:t1" for task X while the transaction engine reserves
        per-action leases for X's actions). Anything else holding the
        ancestor (human, conflict, another task) still refuses.
        """
        ttl = _clamp_ttl(ttl)
        with self._lock:
            if self._estop:
                return None
            # Hierarchy check first: any blocked ancestor blocks the leaf.
            for ancestor in owner_hierarchy:
                st = self.ownership.state_of(ancestor)
                if st in (HUMAN_OWNED, CONFLICT):
                    return None
                if st == AGENT_OWNED:
                    if nest_under_task_id is not None:
                        holder_lease_id = self.ownership.owner_of(
                            ancestor).get("lease")
                        holder = self.leases.get(holder_lease_id) \
                            if holder_lease_id else None
                        if holder is not None and \
                                holder.get("task_id") == nest_under_task_id:
                            continue  # same task: nested lease allowed
                    return None  # ancestor exclusively held: do not nest
            state = self.ownership.state_of(target)
            if state in (HUMAN_OWNED, CONFLICT, AGENT_OWNED):
                return None  # exclusive: another lease (or human) holds it
            lease_id = f"lease_{uuid.uuid4().hex[:12]}"
            if not self.ownership.acquire_agent(target, lease_id, actor, ttl):
                return None  # lost the race between check and transition
            rec = {"lease": lease_id, "target": target, "intent": intent,
                   "actor": actor, "task_id": task_id, "action_id": action_id,
                   "hierarchy": tuple(owner_hierarchy),
                   "acquired_at": time.time(), "expires_at": time.time() + ttl,
                   "ttl": ttl}
            self.leases[lease_id] = rec
            return dict(rec)

    def release(self, target: str, lease_id: str | None = None) -> bool:
        """Compare-and-release.

        Preferred form: release(target, lease_id). For backwards
        compatibility, release(lease_id) looks the target up from the lease
        record; the graph still compares lease ids, so a stale lease can
        never release a newer lease's target. Unknown lease ids fail.
        """
        with self._lock:
            if lease_id is None:
                # Compat: first arg is actually a lease id.
                info = self.leases.pop(target, None)
                if info is None:
                    return False
                return self.ownership.release(info["target"], target)
            info = self.leases.get(lease_id)
            if info is None or info["target"] != target:
                # Unknown lease id, or lease/target mismatch: fail. The
                # manager only releases leases it minted; anything else is
                # either stale (already handled) or foreign.
                return False
            self.leases.pop(lease_id, None)
            return self.ownership.release(target, lease_id)

    def prune(self) -> int:
        """Prune expired leases (and expired ownership records)."""
        with self._lock:
            now = time.time()
            count = 0
            for lid, info in list(self.leases.items()):
                if info["expires_at"] < now:
                    self.leases.pop(lid, None)
                    self.ownership.release(info["target"], lid)
                    count += 1
            count += self.ownership.prune()
            return count

    def active(self) -> list[dict]:
        with self._lock:
            self.prune()
            return [dict(r) for r in self.leases.values()]


def hierarchy_for(tab_id: str | None, frame_id: str | None = None,
                  session_id: str | None = None) -> tuple[str, ...]:
    """Ancestor ownership keys, outermost first: session > tab > frame."""
    ancestors: list[str] = []
    if session_id:
        ancestors.append(f"session:{session_id}")
    if tab_id:
        ancestors.append(f"tab:{tab_id}")
        if frame_id and frame_id != "main":
            ancestors.append(f"frame:{tab_id}:{frame_id}")
    return tuple(ancestors)


class ConflictDetector:
    """Target overlap + intent/precondition conflicts."""

    @staticmethod
    def check(action: dict, world) -> tuple[str, str]:
        """-> (verdict, reason): 'continue' | 'replan' | 'request_ownership'."""
        target = str(action.get("target", action.get("selector",
                      action.get("ref", "?"))))
        ownership = getattr(world, "ownership", None)
        state = ownership.state_of(target) if ownership else FREE
        if state == CONFLICT:
            return "request_ownership", f"ownership conflict on {target}"
        if state == HUMAN_OWNED:
            return "replan", f"human owns {target}"
        # Hierarchy: a human-owned / conflicted / agent-held ancestor blocks.
        if ownership:
            for ancestor in hierarchy_for(action.get("tab_id"),
                                          action.get("frame_id"),
                                          action.get("session_id")):
                astate = ownership.state_of(ancestor)
                if astate == CONFLICT:
                    return "request_ownership", \
                        f"ownership conflict on ancestor {ancestor}"
                if astate == HUMAN_OWNED:
                    return "replan", f"human owns ancestor {ancestor}"
                if astate == AGENT_OWNED and action.get("lease") is None:
                    return "request_ownership", \
                        f"ancestor {ancestor} leased to another agent action"
        # intent-level: preconditions vs current human/world state
        for pre in action.get("preconditions", []):
            if not ConflictDetector._holds(pre, world):
                return "replan", f"precondition failed: {pre}"
        if state == AGENT_OWNED and action.get("lease") is None:
            return "request_ownership", f"{target} leased to another agent action"
        return "continue", "no conflict"

    @staticmethod
    def _holds(precondition: str, world) -> bool:
        # Preconditions are "key == value" over human/page state, e.g.
        # "human.search_query == iphone 16". Unknown keys are treated as holding
        # (fail open on metadata, fail closed on ownership above).
        try:
            key, _, want = precondition.partition("==")
            key, want = key.strip(), want.strip()
            if key.startswith("human."):
                got = world.world.human.get(key[6:], "")
            elif key.startswith("page."):
                got = world.world.page.get(key[5:], "")
            else:
                return True
            return str(got) == want
        except Exception:
            return True


class TransactionRunner:
    """Legacy entrypoint (pre-Stage-C contract), preserved for the headless
    benchmark and older callers.

    Delegates to :class:`harness.transactions.TransactionEngine` in
    non-strict mode and maps the result back to the legacy dict shape
    ``{"ok", "verdict", "error"}``. IMPORTANT: the legacy ``"executed"``
    verdict means "dispatched and acknowledged by the executor" -- it does
    NOT mean the action was independently verified. New code must use
    ``harness.gateway.ExecutionGateway`` / ``TransactionEngine`` in strict
    mode, where VERIFIED requires postcondition-satisfying observation.
    """

    def __init__(self, world, ownership: OwnershipGraph,
                 leases: LeaseManager, tools, safety):
        # Late import: transactions.py imports names from this module.
        from .transactions import TransactionEngine
        from .context_manager import ContextManager
        from .verification.verifier import Verifier
        self.world = world
        self.engine = TransactionEngine(
            world, ownership, leases, tools, safety,
            ContextManager(), Verifier(world))

    def run(self, action: dict, page_url: str = "",
            user_consented: bool = False, tab_id: str = "default") -> dict:
        from .transactions import (CONFLICTING, FAILED, OWNERSHIP_CONFLICT,
                                   UNVERIFIED, VERIFIED)
        report = self.engine.execute(
            [action], task_id=action.get("task_id") or "legacy",
            tab_id=action.get("tab_id", tab_id), page_url=page_url,
            user_consented=user_consented, strict=False)
        ex = report.executions[0]
        d = ex.to_dict()
        if ex.state == CONFLICTING:
            verdict = getattr(ex, "conflict_verdict", None) \
                or ("request_ownership"
                    if ex.error_code == OWNERSHIP_CONFLICT else "replan")
            return {"ok": False, "verdict": verdict, "error": ex.error,
                    "error_code": ex.error_code, "state": ex.state}
        if ex.state == FAILED:
            return {"ok": False, "verdict": "failed", "error": ex.error,
                    "error_code": ex.error_code, "state": ex.state,
                    "verification": ex.verification}
        # ACKNOWLEDGED / VERIFIED / UNVERIFIED: the executor accepted the
        # command (legacy "executed" semantic; see class docstring).
        return {"ok": True, "verdict": "executed",
                "command": d.get("command"), "args": d.get("args"),
                "state": ex.state, "claim_id": ex.claim_id,
                "verification": ex.verification}
