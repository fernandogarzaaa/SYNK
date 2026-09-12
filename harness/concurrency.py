"""Concurrency layer: shared-state co-execution (Beta §2-4).

Replaces "human priority -> pause agent" (serialized) with:
  Human and agent operate simultaneously unless intended state changes conflict.

Primitives:
- Ownership graph: FREE / HUMAN_OWNED / AGENT_OWNED / CONFLICT per target.
- Short-lived agent leases (500ms-5s), never permanent locks.
- Intent-level conflict detection: an agent action is invalid when its
  *precondition* no longer holds, even on a different DOM node
  (e.g. human changed search_query the agent planned to submit).
- Transactional execution: READ -> PLAN -> RESERVE -> VALIDATE -> EXECUTE -> VERIFY.
"""
from __future__ import annotations

import time
import uuid

FREE, HUMAN_OWNED, AGENT_OWNED, CONFLICT = "FREE", "HUMAN_OWNED", "AGENT_OWNED", "CONFLICT"

HUMAN_TTL = 5.0  # human ownership decays; the human is not a lock either


class OwnershipGraph:
    def __init__(self):
        self.nodes: dict[str, dict] = {}  # target -> {state, owner, until}

    def _get(self, target: str) -> dict:
        n = self.nodes.get(target, {"state": FREE, "owner": None, "until": 0})
        if n["state"] != FREE and n["until"] < time.time():
            return {"state": FREE, "owner": None, "until": 0}
        return n

    def state_of(self, target: str) -> str:
        return self._get(target)["state"]

    def mark_human(self, target: str, ttl: float = HUMAN_TTL) -> None:
        cur = self._get(target)
        state = CONFLICT if cur["state"] == AGENT_OWNED else HUMAN_OWNED
        self.nodes[target] = {"state": state, "owner": "human",
                              "until": time.time() + ttl}

    def mark_agent(self, target: str, lease: str, ttl: float) -> None:
        self.nodes[target] = {"state": AGENT_OWNED, "owner": "agent",
                              "lease": lease, "until": time.time() + ttl}

    def release(self, target: str) -> None:
        self.nodes.pop(target, None)

    def snapshot(self) -> dict:
        now = time.time()
        return {t: n["state"] for t, n in self.nodes.items() if n["until"] >= now}


class LeaseManager:
    """Short-lived action leases: {actor, target, intent, lease, ttl}."""

    def __init__(self, ownership: OwnershipGraph):
        self.ownership = ownership
        self.leases: dict[str, dict] = {}

    def acquire(self, target: str, intent: str = "", ttl: float = 2.0) -> dict | None:
        state = self.ownership.state_of(target)
        if state in (HUMAN_OWNED, CONFLICT):
            return None  # uncertain/conflict -> caller must replan or request ownership
        lease = f"lease_{uuid.uuid4().hex[:6]}"
        self.leases[lease] = {"lease": lease, "target": target,
                              "intent": intent, "expires_at": time.time() + ttl}
        self.ownership.mark_agent(target, lease, ttl)
        return self.leases[lease]

    def release(self, lease: str) -> None:
        info = self.leases.pop(lease, None)
        if info:
            self.ownership.release(info["target"])

    def prune(self) -> None:
        now = time.time()
        for lid, info in list(self.leases.items()):
            if info["expires_at"] < now:
                self.release(lid)


class ConflictDetector:
    """Target overlap + intent/precondition conflicts (Beta §4)."""

    @staticmethod
    def check(action: dict, world) -> tuple[str, str]:
        """-> (verdict, reason): 'continue' | 'replan' | 'request_ownership'."""
        target = str(action.get("target", action.get("selector",
                      action.get("ref", "?"))))
        state = world.ownership.state_of(target) if hasattr(world, "ownership") else FREE
        if state == CONFLICT:
            return "request_ownership", f"ownership conflict on {target}"
        if state == HUMAN_OWNED:
            return "replan", f"human owns {target}"
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
    """READ -> PLAN -> RESERVE -> VALIDATE PRECONDITIONS -> EXECUTE -> VERIFY."""

    def __init__(self, world, ownership: OwnershipGraph,
                 leases: LeaseManager, tools, safety):
        self.world = world            # WorldState
        self.ownership = ownership
        self.leases = leases
        self.tools = tools            # ToolExecutor
        self.safety = safety          # SafetyLayer

    def run(self, action: dict, page_url: str = "",
            user_consented: bool = False, tab_id: str = "default") -> dict:
        target = str(action.get("target", action.get("selector",
                       action.get("ref", "?"))))
        # RESERVE
        lease = self.leases.acquire(target, action.get("intent", ""),
                                    ttl=float(action.get("lease_ttl", 2.0)))
        if lease is None:
            self.safety.log(action, "denied:ownership-conflict")
            return {"ok": False, "verdict": "request_ownership",
                    "error": f"cannot reserve {target}: human-owned or conflicted"}
        action = {**action, "lease": lease["lease"]}
        # VALIDATE PRECONDITIONS (intent-level)
        verdict, reason = ConflictDetector.check(
            action, _WorldView(self.world, self.ownership))
        if verdict != "continue":
            self.leases.release(lease["lease"])
            self.safety.log(action, f"denied:{verdict}:{reason}")
            return {"ok": False, "verdict": verdict, "error": reason}
        # EXECUTE via existing guarded tools (safety allowlist still applies)
        tool_action = {"tool": action.get("tool", action.get("command", "")),
                       **{k: v for k, v in action.items()
                          if k not in ("target", "intent", "preconditions")}}
        res = self.tools.run(tool_action, page_url, user_consented, tab_id=tab_id)
        # VERIFY
        verified = res.get("ok", False)
        for check in action.get("verification", []):
            if not self._verify(check):
                verified = False
                res = {**res, "ok": False, "verify_failed": check}
        self.leases.release(lease["lease"])
        res["verdict"] = "executed" if verified else "failed-verification"
        return res

    def _verify(self, check: str) -> bool:
        # Minimal verifiers; unknown checks pass (extension confirms visually).
        if check == "no_modal_blocking":
            return not self.world.interaction.get("modal")
        return True


class _WorldView:
    """Adapter so ConflictDetector sees world + ownership together."""

    def __init__(self, world, ownership):
        self.world = world
        self.ownership = ownership
