"""Transaction engine: honest, per-action execution lifecycle (Stage C).

Central invariant: an action is VERIFIED only when independent runtime
observation satisfies its declared postcondition. The engine implements the
full lifecycle per action:

    REQUEST -> VALIDATE -> RESERVE -> PRECONDITION CHECK -> DISPATCH
        -> EXECUTION ACK -> OBSERVE -> VERIFY -> COMMIT

Action states: PENDING, VALIDATED, LEASED, DISPATCHED, ACKNOWLEDGED,
OBSERVING, VERIFIED, FAILED, UNVERIFIED, CONFLICTING, CANCELLED.

Typed error taxonomy (used everywhere, including the legacy
TransactionRunner wrapper): POLICY_DENIED, CONSENT_REQUIRED,
OWNERSHIP_CONFLICT, STALE_REFERENCE, BROWSER_NOT_READY, ACTION_FAILED,
TIMEOUT, NAVIGATION_CHANGED, VERIFICATION_FAILED, VERIFICATION_UNAVAILABLE,
TOOL_NOT_FOUND, SCHEMA_INVALID.

An execution failure is NEVER reported as a verification failure: when the
tool call itself fails, no claim is proposed and the action ends FAILED with
error_code ACTION_FAILED (or a more specific code). Verification only runs
against acknowledged executions.

Semantics note: this is "transactional orchestration with explicit
partial-commit semantics", NOT ACID transactions. Browser DOM mutations
cannot be rolled back; the aggregate TransactionVerification therefore
reports COMMITTED / PARTIALLY_COMMITTED / FAILED / UNVERIFIED, and
ROLLED_BACK only when compensating rollback handlers actually ran
(currently none exist, so ROLLED_BACK is never produced by this engine).

Stdlib only. No Python hash() for identity (sha256 via session.stable_id).
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Callable

from .concurrency import (ConflictDetector,
                          LeaseManager, OwnershipGraph, hierarchy_for)
from .session import new_id
from .verification.claims import Claim
from .verification.evidence import (BROWSER_EVENT, Evidence)
from .verification.results import (CONFLICTING, FAILED, UNVERIFIED, VERIFIED,
                                   VerificationResult)

# -- typed error taxonomy -------------------------------------------------------
POLICY_DENIED = "POLICY_DENIED"
CONSENT_REQUIRED = "CONSENT_REQUIRED"
OWNERSHIP_CONFLICT = "OWNERSHIP_CONFLICT"
STALE_REFERENCE = "STALE_REFERENCE"
BROWSER_NOT_READY = "BROWSER_NOT_READY"
ACTION_FAILED = "ACTION_FAILED"
TIMEOUT = "TIMEOUT"
NAVIGATION_CHANGED = "NAVIGATION_CHANGED"
VERIFICATION_FAILED = "VERIFICATION_FAILED"
VERIFICATION_UNAVAILABLE = "VERIFICATION_UNAVAILABLE"
TOOL_NOT_FOUND = "TOOL_NOT_FOUND"
SCHEMA_INVALID = "SCHEMA_INVALID"

ERROR_CODES = (
    POLICY_DENIED, CONSENT_REQUIRED, OWNERSHIP_CONFLICT, STALE_REFERENCE,
    BROWSER_NOT_READY, ACTION_FAILED, TIMEOUT, NAVIGATION_CHANGED,
    VERIFICATION_FAILED, VERIFICATION_UNAVAILABLE, TOOL_NOT_FOUND,
    SCHEMA_INVALID,
)

# -- action states --------------------------------------------------------------
# Honest lifecycle: REQUEST -> VALIDATED -> LEASED -> PRECONDITION_CHECK ->
# DISPATCHED -> ACKNOWLEDGED -> OBSERVING -> VERIFIED (-> COMMIT).
# VERIFIED is the action-level commit: the claim was confirmed by an
# independent observation. PENDING is the pre-request placeholder.
REQUEST = "REQUEST"
PENDING = "PENDING"
VALIDATED = "VALIDATED"
LEASED = "LEASED"
PRECONDITION_CHECK = "PRECONDITION_CHECK"
DISPATCHED = "DISPATCHED"
ACKNOWLEDGED = "ACKNOWLEDGED"
OBSERVING = "OBSERVING"
VERIFIED = "VERIFIED"        # state name (distinct from results.VERIFIED value)
FAILED = "FAILED"
UNVERIFIED = "UNVERIFIED"
CONFLICTING = "CONFLICTING"
CANCELLED = "CANCELLED"

# -- aggregate transaction statuses ----------------------------------------------
TX_COMMITTED = "COMMITTED"
TX_PARTIALLY_COMMITTED = "PARTIALLY_COMMITTED"
TX_FAILED = "FAILED"
TX_UNVERIFIED = "UNVERIFIED"
TX_ROLLED_BACK = "ROLLED_BACK"


# -- postconditions ----------------------------------------------------------------
def postcondition_for(action: dict) -> dict | None:
    """Declared postcondition for a tool action, or None if it has none.

    The verifier only accepts evidence appropriate to this postcondition;
    a bare "the executor said ok" (BROWSER_EVENT) can never satisfy it.
    """
    tool = action.get("tool", action.get("action", ""))
    target = action.get("target", action.get("selector", action.get("ref")))
    if tool == "type":
        return {"kind": "element_value", "target": target,
                "value": action.get("text")}
    if tool == "select":
        return {"kind": "element_value", "target": target,
                "value": action.get("value")}
    if tool == "click":
        return {"kind": "element_interaction", "target": target}
    if tool == "navigate":
        return {"kind": "url", "url": action.get("url")}
    if tool == "press_key":
        return {"kind": "element_interaction", "target": target,
                "key": action.get("key")}
    if tool in ("hover", "focus"):
        return {"kind": "element_interaction", "target": target}
    if tool == "webmcp_invoke":
        # Stage E: the claim is "the page's model-context tool reported
        # success". Verified only by a page-reported WEBMCP_RESULT naming
        # this tool (fixture-fallback results are excluded by the
        # verifier).
        return {"kind": "webmcp_result",
                "tool_name": action.get("tool_name")}
    return None


def claimed_state_for_action(action: dict):
    """Concrete claimed_state for common tools, else None."""
    tool = action.get("tool", action.get("action", ""))
    if tool == "navigate":
        return action.get("url")
    if tool == "type":
        return action.get("text")
    if tool == "select":
        return action.get("value")
    if tool in ("click", "hover", "focus"):
        return action.get("target", action.get("selector", action.get("ref")))
    return None


# -- records -----------------------------------------------------------------------
@dataclass
class ActionRequest:
    action: dict
    task_id: str
    transaction_id: str
    action_id: str
    tab_id: str = "default"
    session_id: str | None = None
    window_id: str = "win_default"
    frame_id: str = "main"
    page_url: str = ""
    user_consented: bool = False


@dataclass
class ActionExecution:
    """One action's journey through the lifecycle, with full history."""
    request: ActionRequest
    state: str = PENDING
    history: list = field(default_factory=list)  # [(state, ts, note)]
    lease_id: str | None = None
    error_code: str | None = None
    error: str | None = None
    tool_result: dict | None = None
    claim_id: str | None = None
    verification: dict | None = None  # VerificationResult.to_dict()
    pre_observation: dict | None = None
    conflict_verdict: str | None = None  # legacy replan/request_ownership hint

    def transition(self, state: str, note: str = "") -> None:
        self.state = state
        self.history.append((state, time.time(), note))

    def to_dict(self) -> dict:
        d = {"action_id": self.request.action_id,
             "tool": self.request.action.get("tool"),
             "target": self.request.action.get("target",
                        self.request.action.get("selector",
                        self.request.action.get("ref"))),
             "state": self.state,
             "error_code": self.error_code,
             "error": self.error,
             "lease_id": self.lease_id,
             "claim_id": self.claim_id,
             "verification": self.verification,
             "history": [(s, round(t, 3), n) for s, t, n in self.history]}
        if self.tool_result:
            d["command"] = self.tool_result.get("command")
            d["args"] = self.tool_result.get("args")
            d["ok"] = self.tool_result.get("ok")
            # Stage E: surface the WebMCP partial-fallback marker so
            # callers can see a fixture answered (it never verifies).
            if "partial_fallback" in self.tool_result:
                d["partial_fallback"] = self.tool_result["partial_fallback"]
            if self.tool_result.get("caveat"):
                d["caveat"] = self.tool_result["caveat"]
        return d


@dataclass
class TransactionReport:
    transaction_id: str
    task_id: str
    executions: list  # [ActionExecution]
    status: str = TX_UNVERIFIED

    def to_dict(self) -> dict:
        return {"transaction_id": self.transaction_id,
                "task_id": self.task_id,
                "status": self.status,
                "actions": [e.to_dict() for e in self.executions]}


class _WorldView:
    """Adapter so ConflictDetector sees world + ownership together."""

    def __init__(self, world, ownership):
        self.world = world
        self.ownership = ownership


class TransactionEngine:
    """Canonical per-action transaction engine (Stage C).

    Dependencies are explicit constructor args; `emit` is an optional
    callable(type, data) for bus fan-out (server passes STATE.bus.emit).
    """

    def __init__(self, world, ownership: OwnershipGraph,
                 leases: LeaseManager, tools, safety, ctx, verifier,
                 emit: Callable[[str, dict], Any] | None = None,
                 policy=None, sessions=None):
        self.world = world
        self.ownership = ownership
        self.leases = leases
        self.tools = tools
        self.safety = safety
        self.ctx = ctx                # ContextManager (fail-closed refs)
        self.verifier = verifier      # the ONE global Verifier
        self.emit = emit or (lambda t, d: None)
        # Stage F: per-origin execution policy (default-deny). A None
        # policy builds an empty registry (every known origin fails
        # closed; no-origin calls abstain to the legacy safety path).
        from .policy import OriginPolicyRegistry
        self.policy = policy or OriginPolicyRegistry(emit=self.emit)
        self.sessions = sessions      # SessionManager (origin resolution)

    # -- public ------------------------------------------------------------------
    def prepare(self, action: dict, *, task_id: str,
                tab_id: str = "default", session_id: str | None = None,
                window_id: str = "win_default", frame_id: str = "main",
                page_url: str = "", user_consented: bool = False,
                ttl: float = 10.0) -> ActionExecution:
        """Run REQUEST -> VALIDATE -> RESERVE -> PRECONDITION CHECK only.

        Returns the ActionExecution in LEASED state with the action mutated
        to carry ``lease`` (lease id), ``lease_ttl`` and ``action_id`` -- or
        in a terminal FAILED/CONFLICTING state. The caller (e.g. the
        closed-loop agent) later submits the action through
        :meth:`execute` / the gateway, which adopts the held lease instead
        of acquiring a new one. Terminal failures release any lease before
        returning.
        """
        req = ActionRequest(
            action=dict(action), task_id=task_id,
            transaction_id=new_id("tx"),
            action_id=action.get("action_id") or new_id("a"),
            tab_id=action.get("tab_id", tab_id),
            session_id=action.get("session_id", session_id),
            window_id=action.get("window_id", window_id),
            frame_id=action.get("frame_id", frame_id),
            page_url=page_url,
            user_consented=bool(action.get("user_consented", user_consented)))
        req.action["action_id"] = req.action_id
        req.action.setdefault("lease_ttl", ttl)
        ex = ActionExecution(request=req)
        ex.transition(REQUEST, "request received")
        if self._validate_reserve_precondition(ex):
            return ex  # terminal FAILED/CONFLICTING (lease released inside)
        req.action["lease"] = ex.lease_id
        # Closed-loop path: propose the claim now so the loop's later
        # report() has something to verify against the post-execution
        # observation. Verification itself happens after the client pushes
        # the post-action snapshot (never here).
        self._propose_claim(ex, strict=True)
        ex.transition(OBSERVING, "claim proposed; awaiting browser execution")
        return ex

    def execute(self, actions: list[dict], *, task_id: str,
                tab_id: str = "default", session_id: str | None = None,
                window_id: str = "win_default", frame_id: str = "main",
                page_url: str = "", user_consented: bool = False,
                strict: bool = True) -> TransactionReport:
        """Run a batch; every action gets its own lifecycle + claim.

        strict=True (canonical gateway): postconditions enforced, unknown
        verification checks fail closed. strict=False is the legacy
        TransactionRunner semantic (dispatch acknowledgement only),
        preserved for benchmark compatibility.
        """
        transaction_id = new_id("tx")
        report = TransactionReport(transaction_id=transaction_id,
                                   task_id=task_id, executions=[])
        # Bulk expansion: one lifecycle per sub-action (no vague batch claim).
        expanded: list[dict] = []
        for a in actions:
            if isinstance(a, dict) and a.get("tool") == "bulk" \
                    and isinstance(a.get("actions"), list):
                expanded.extend(a["actions"])
            else:
                expanded.append(a)
        for i, action in enumerate(expanded):
            req = ActionRequest(
                action=dict(action),
                task_id=task_id, transaction_id=transaction_id,
                action_id=action.get("action_id") or f"{transaction_id}:a{i}",
                tab_id=action.get("tab_id", tab_id),
                session_id=action.get("session_id", session_id),
                window_id=action.get("window_id", window_id),
                frame_id=action.get("frame_id", frame_id),
                page_url=page_url,
                user_consented=bool(action.get("user_consented",
                                               user_consented)),
            )
            report.executions.append(self._execute_one(req, strict=strict))
        report.status = self._aggregate(report.executions)
        return report

    # -- aggregate -----------------------------------------------------------------
    @staticmethod
    def _aggregate(executions: list[ActionExecution]) -> str:
        if not executions:
            return TX_UNVERIFIED
        states = [e.state for e in executions]
        n_verified = sum(1 for s in states if s == VERIFIED)
        # FAILED aggregates hard failures AND contention/cancellation: the
        # transaction achieved nothing verifiable because actions could not
        # run (validation, dispatch, ownership conflict, cancellation).
        # UNVERIFIED is reserved for "dispatched and acknowledged, but no
        # independent observation has confirmed the postcondition yet".
        n_failed = sum(1 for s in states
                       if s in (FAILED, CONFLICTING, CANCELLED))
        if n_verified == len(executions):
            return TX_COMMITTED
        if n_verified > 0:
            return TX_PARTIALLY_COMMITTED
        if n_failed > 0:
            return TX_FAILED
        return TX_UNVERIFIED

    # -- single-action lifecycle -----------------------------------------------------
    def _execute_one(self, req: ActionRequest,
                     strict: bool) -> ActionExecution:
        ex = ActionExecution(request=req)
        ex.transition(REQUEST, "request received")
        if self._validate_reserve_precondition(ex):
            return ex  # terminal FAILED/CONFLICTING
        try:
            # DISPATCH via the guarded tool executor.
            action = ex.request.action
            tab_id = ex.request.tab_id
            tool = action.get("tool", action.get("action", ""))
            ex.transition(DISPATCHED, "sent to tool executor")
            tool_action = {"tool": tool,
                           **{k: v for k, v in action.items()
                              if k not in ("target", "intent", "preconditions",
                                           "verification", "lease_ttl",
                                           "ref_version")}}
            # Stage E: stamp the request's identity onto the dispatched
            # action. The tool executor only sees this dict; webmcp_invoke
            # needs (session, tab, frame) to scope its handle, and its
            # evidence needs (task_id, action_id) to tie to the claim.
            tool_action.setdefault("session_id", req.session_id)
            tool_action.setdefault("frame_id", req.frame_id)
            tool_action.setdefault("task_id", req.task_id)
            tool_action["action_id"] = req.action_id
            res = self.tools.run(tool_action, req.page_url,
                                 req.user_consented, tab_id=tab_id)
            ex.tool_result = res
            if not res.get("ok"):
                ex.transition(FAILED, "tool reported failure")
                ex.error_code, ex.error = self._classify_tool_error(res, tool)
                self.safety.log(action, f"failed:{ex.error_code}:{ex.error}")
                return ex
            # ACK: the executor accepted the command. In extension mode this
            # means "validated + queued for the content script", NOT "the
            # browser performed it" -- honesty enforced by never marking
            # VERIFIED here.
            ex.transition(ACKNOWLEDGED, "executor accepted command")

            # OBSERVE: capture pre/post world observation + record the
            # command-accepted evidence (strength: weakest).
            ex.transition(OBSERVING, "capturing observation")
            ex.pre_observation = self._tab_observation(tab_id)
            self._record_dispatch_evidence(ex, res)

            # VERIFY: per-action claim through the ONE global verifier.
            ex.transition(OBSERVING, "verifying")
            self._verify_action(ex, strict=strict)
            return ex
        finally:
            if ex.lease_id:
                # Match _reserve(): lease keys come from _lease_target_for.
                self.leases.release(
                    _lease_target_for(req.action, req.tab_id),
                    ex.lease_id)

    # -- lifecycle helpers -------------------------------------------------------------
    def _validate_reserve_precondition(self, ex: ActionExecution) -> bool:
        """Shared REQUEST -> VALIDATE -> RESERVE -> PRECONDITION CHECK.

        Returns True when the execution reached a terminal FAILED /
        CONFLICTING state (any acquired lease is released before return),
        False when it is LEASED and ready to dispatch.
        """
        req = ex.request
        action = req.action

        # VALIDATE: schema + fail-closed element refs.
        tool = action.get("tool", action.get("action", ""))
        from .tools import TOOL_SCHEMAS
        if tool not in TOOL_SCHEMAS:
            self._fail(ex, TOOL_NOT_FOUND,
                       f"unknown tool '{tool}' (not in allowlist)")
            return True
        if tool != "bulk":
            missing = [k for k in TOOL_SCHEMAS[tool]
                       if k not in action or action[k] is None]
            # 'ref' may be satisfied by an equivalent selector/target.
            missing = [k for k in missing
                       if not (k == "ref" and action.get("selector")) and
                       not (k == "ref" and action.get("target"))]
            if missing:
                self._fail(ex, SCHEMA_INVALID,
                           f"tool '{tool}' missing required args: {missing}")
                return True
        ref_err = self._validate_ref(action, req)
        if ref_err:
            ex.transition(CONFLICTING, ref_err)
            ex.error_code = STALE_REFERENCE
            ex.error = ref_err
            return True

        # POLICY (Stage F): per-origin policy decision BEFORE lease
        # acquisition and dispatch. Unknown origins fail closed
        # (POLICY_DENIED); an undeterminable origin abstains to the legacy
        # safety path (unit tests with no page context).
        from .policy import origin_of_url, POLICY_ABSTAIN
        origin = origin_of_url(req.page_url)
        if not origin and self.sessions is not None:
            try:
                origin = origin_of_url(
                    self.sessions.tab_url(req.tab_id,
                                          session_id=req.session_id) or "")
            except Exception:
                origin = ""
        action_class = self.policy.action_class_for(tool)
        decision = self.policy.check_action(
            req.task_id, origin, action_class,
            user_consented=req.user_consented, tool_name=tool)
        self.safety.log(
            action,
            f"policy:{decision.error_code or 'allowed'}:{decision.reason}")
        if decision.error_code != POLICY_ABSTAIN and not decision.allowed:
            self._fail(ex, decision.error_code,
                       f"policy: {decision.reason}")
            return True
        ex.transition(VALIDATED, "schema + refs + policy ok")

        # RESERVE: exclusive, hierarchy-aware lease (or adopt a pre-held one).
        lease = self._reserve(req)
        if lease is None:
            ex.transition(CONFLICTING, "lease refused")
            ex.error_code = OWNERSHIP_CONFLICT
            ex.error = ("cannot reserve target: held by another lease, "
                        "human-owned, conflicted, or emergency stop active")
            ex.conflict_verdict = "request_ownership"  # legacy mapping aid
            self.safety.log(action, "denied:ownership-conflict")
            return True
        ex.lease_id = lease["lease"]
        action = {**action, "lease": lease["lease"]}
        req.action = action
        ex.transition(LEASED, f"lease {lease['lease']}")
        ex.transition(PRECONDITION_CHECK, "checking intent preconditions")

        # PRECONDITION CHECK (intent-level, fail closed on ownership).
        verdict, reason = ConflictDetector.check(
            action, _WorldView(self.world, self.ownership))
        if verdict != "continue":
            ex.transition(CONFLICTING, reason)
            ex.error_code = OWNERSHIP_CONFLICT
            ex.error = reason
            ex.conflict_verdict = verdict  # legacy mapping aid
            self.safety.log(action, f"denied:{verdict}:{reason}")
            self.leases.release(
                _lease_target_for(action, req.tab_id), ex.lease_id)
            ex.lease_id = None
            return True
        return False

    def _fail(self, ex: ActionExecution, code: str,
              msg: str) -> ActionExecution:
        ex.transition(FAILED, msg)
        ex.error_code = code
        ex.error = msg
        self.safety.log(ex.request.action, f"denied:{code}:{msg}")
        return ex

    def _validate_ref(self, action: dict, req: ActionRequest) -> str | None:
        """Fail-closed element reference check. Returns error or None."""
        ref = action.get("ref")
        if not isinstance(ref, int):
            return None  # selector/target addressing; nothing to resolve
        version = action.get("ref_version")
        if version is None:
            # No pinned version: resolve against the tab's CURRENT snapshot.
            # Refs from any older snapshot fail closed here.
            version = self.ctx.tab_version(req.tab_id)
        origin = None
        try:
            from .session import canonical_origin
            origin = canonical_origin(req.page_url)
        except Exception:
            pass
        meta = self.ctx.resolve_ref(ref, version, tab_id=req.tab_id,
                                    frame_id=req.frame_id,
                                    origin=origin or None)
        if meta is None:
            return (f"stale or unresolvable ref {ref} for tab "
                    f"{req.tab_id} (snapshot v{version})")
        # Pin the resolved selector so dispatch never re-resolves a ref.
        # The pinned selector is only valid for the pinned snapshot version;
        # refs from any older snapshot fail closed above.
        locator = meta.get("locator", {})
        if not action.get("selector") and locator.get("value") and \
                locator.get("strategy") in ("test-id", "css-id", "css"):
            action["selector"] = locator["value"]
        action["ref_version"] = version
        return None

    def _reserve(self, req: ActionRequest) -> dict | None:
        action = req.action
        # Stage E: WebMCP invocations lease the tool handle in the tab, not
        # a DOM node (see _lease_target_for). Every release site uses the
        # same helper so acquire/release keys always match.
        target = _lease_target_for(action, req.tab_id)
        held = action.get("lease")
        if held:
            # Adopt a lease pre-acquired by /agent/step: it must still be
            # live, held by us, and on this exact target.
            rec = self.leases.leases.get(held)
            if rec and rec["target"] == target and \
                    rec["expires_at"] > time.time():
                return rec
            return None
        hierarchy = hierarchy_for(req.tab_id, req.frame_id, req.session_id)
        return self.leases.acquire(
            target, action.get("intent", ""),
            ttl=_bounded(float(action.get("lease_ttl", 2.0)), 0.1, 30.0),
            actor="agent", task_id=req.task_id,
            action_id=req.action_id, owner_hierarchy=hierarchy)

    @staticmethod
    def _classify_tool_error(res: dict, tool: str) -> tuple[str, str]:
        # Stage E: executors that classify their own failures (the WebMCP
        # gateway) attach a taxonomy code; honor it verbatim.
        code = res.get("error_code")
        if code in ERROR_CODES:
            return code, str(res.get("error", "tool failed"))
        err = str(res.get("error", "tool failed"))
        if res.get("consent_required") or "consent_required" in err:
            return CONSENT_REQUIRED, err
        if err.startswith("denied:"):
            return POLICY_DENIED, err
        if "unknown-tool" in err:
            return TOOL_NOT_FOUND, err
        if "cdp" in err.lower() or "browser" in err.lower():
            return BROWSER_NOT_READY, err
        return ACTION_FAILED, err

    def _tab_observation(self, tab_id: str) -> dict | None:
        try:
            return self.world.tab_observation(tab_id)
        except Exception:
            return None

    def _record_dispatch_evidence(self, ex: ActionExecution,
                                  res: dict) -> None:
        """BROWSER_EVENT: command accepted. Weakest evidence tier, recorded
        honestly -- it proves dispatch, never browser execution."""
        import uuid as _uuid
        req = ex.request
        action = req.action
        target = str(action.get("target", action.get("selector",
                       action.get("ref", "?"))))
        try:
            self.verifier.record_evidence(Evidence(
                evidence_id=f"e_{_uuid.uuid4().hex[:8]}",
                evidence_type=BROWSER_EVENT,
                source="runtime", timestamp=time.time(),
                action_id=req.action_id, task_id=req.task_id,
                world_state_version=getattr(self.world, "version", None),
                payload={"command": action.get("tool"), "target": target,
                         "ok": res.get("ok"), "tab_id": req.tab_id,
                         "browser_executed": False,
                         "note": "command accepted by executor; browser "
                                 "execution not yet observed"},
            ))
        except Exception:
            pass
        self.emit("agent.action",
                  {"command": res.get("command", action.get("tool")),
                   "target": target, "ok": res.get("ok"),
                   "task_id": req.task_id, "action_id": req.action_id,
                   "tab_id": req.tab_id, "state": ex.state})

    # -- verification ----------------------------------------------------------------------
    def _propose_claim(self, ex: ActionExecution,
                       strict: bool) -> Claim:
        """Propose the per-action claim through the global verifier.

        Used both by the synchronous path (_verify_action) and by
        prepare() for the closed-loop path, where verification happens
        later against the post-execution observation.
        """
        req = ex.request
        action = req.action
        claim_id = action.get("claim_id") or new_id("c")
        ex.claim_id = claim_id
        postcondition = postcondition_for(action) if strict else None
        claim = Claim(
            claim_id=claim_id, task_id=req.task_id, actor="agent",
            claim_type="ACTION_COMPLETED",
            target=str(action.get("target", action.get("selector",
                         action.get("ref", "?")))),
            requested_state={k: v for k, v in action.items()
                             if k not in ("verification",)},
            claimed_state=claimed_state_for_action(action),
            action_ids=[req.action_id],
            tool=action.get("tool", ""),
            postcondition=postcondition,
        )
        self.verifier.propose_claim(claim)
        return claim

    def _verify_action(self, ex: ActionExecution, strict: bool) -> None:
        """Per-action claim -> the single global verifier.

        Execution failures never reach here (they return FAILED above), so
        a FAILED verification result always means "ran, but the postcondition
        is not satisfied" -- never "the tool call failed".
        """
        req = ex.request
        action = req.action

        # Named verification checks from the action (fail closed).
        for check in action.get("verification", []) or []:
            ok, detail = self._run_check(check, req, ex)
            if ok is False:
                ex.verification = VerificationResult(
                    FAILED, reason=f"check '{check}' contradicted: {detail}",
                    timestamp=time.time()).to_dict()
                ex.transition(FAILED, f"verification check failed: {check}")
                ex.error_code = VERIFICATION_FAILED
                ex.error = detail
                self._emit_verified(ex)
                return
            if ok is None:
                # Unknown or indeterminate check: fail closed as UNVERIFIED.
                ex.verification = VerificationResult(
                    UNVERIFIED,
                    reason=f"verification check '{check}' is unknown or "
                           f"indeterminate; failing closed",
                    timestamp=time.time()).to_dict()
                ex.transition(UNVERIFIED, f"check unresolved: {check}")
                ex.error_code = VERIFICATION_UNAVAILABLE
                ex.error = f"unknown verification check: {check}"
                self._emit_verified(ex)
                return

        self._propose_claim(ex, strict)
        claim_id = ex.claim_id
        result = self.verifier.verify(claim_id)
        # Carry the claim identity with the verification so API consumers
        # can join verifications back to actions without extra lookups.
        ex.verification = {**result.to_dict(), "claim_id": claim_id}
        if result.result == VERIFIED:
            ex.transition(VERIFIED, result.reason)
        elif result.result == CONFLICTING:
            ex.transition(FAILED, result.reason)
            ex.error_code = VERIFICATION_FAILED
            ex.error = result.reason
        elif result.result == FAILED:
            ex.transition(FAILED, result.reason)
            ex.error_code = VERIFICATION_FAILED
            ex.error = result.reason
        else:
            ex.transition(UNVERIFIED, result.reason)
            ex.error_code = None  # not an error: awaiting observation
            ex.error = result.reason
        self._emit_verified(ex)

    def _emit_verified(self, ex: ActionExecution) -> None:
        req = ex.request
        self.emit("agent.action_verified",
                  {"claim_id": ex.claim_id, "task_id": req.task_id,
                   "action_id": req.action_id,
                   "result": (ex.verification or {}).get("result"),
                   "state": ex.state, "tab_id": req.tab_id})

    # -- named verification checks (fail closed) ----------------------------------------------
    def _run_check(self, check: str, req: ActionRequest,
                   ex: ActionExecution) -> tuple[bool | None, str]:
        """Run one named verification check.

        Returns (True, detail) pass, (False, detail) contradicted,
        (None, detail) unknown/indeterminate -> caller fails closed.
        """
        name, _, param = check.partition("=")
        name, param = name.strip(), param.strip()
        tab_id = req.tab_id
        action = req.action
        target = str(action.get("target", action.get("selector",
                       action.get("ref", "?"))))
        world = self.world

        def _el_value(tgt: str):
            try:
                return world.element_value(tab_id, tgt)
            except Exception:
                return None

        def _el_state(tgt: str):
            try:
                return world.element_state(tab_id, tgt)
            except Exception:
                return None

        def _tab_url() -> str:
            try:
                tab = world.tabs.get(tab_id, {})
                return tab.get("url", "")
            except Exception:
                return ""

        if name == "no_modal_blocking":
            modal = None
            try:
                modal = world.tabs.get(tab_id, {}).get("modal")
            except Exception:
                pass
            if modal:
                return False, f"modal dialog blocking: {modal}"
            return True, "no modal dialog present"

        if name == "value_matches":
            want = param or action.get("text", action.get("value"))
            got = _el_value(target)
            if got is None:
                return None, f"no observed value for {target}"
            if str(got) == str(want):
                return True, f"value matches for {target}"
            return False, f"value {got!r} != expected {want!r} for {target}"

        if name == "url_is":
            want = param or action.get("url", "")
            got = _tab_url()
            if got == want:
                return True, f"url is {got}"
            return False, f"url {got!r} != expected {want!r}"

        if name == "element_present":
            st = _el_state(param or target)
            if st is None:
                return None, f"no observation of {param or target}"
            return True, f"element present: {param or target}"

        if name == "element_absent":
            st = _el_state(param or target)
            if st is None:
                return True, f"element absent: {param or target}"
            return False, f"element still present: {param or target}"

        if name == "navigated":
            before = (ex.pre_observation or {}).get("url", "")
            got = _tab_url()
            if got and got != before:
                return True, f"navigated {before!r} -> {got!r}"
            if not got:
                return None, "no url observation available"
            return False, f"url unchanged ({got!r})"

        return None, f"unknown verification check '{name}'"


def _bounded(v: float, lo: float, hi: float) -> float:
    try:
        v = float(v)
    except (TypeError, ValueError):
        v = lo
    return max(lo, min(hi, v))


def _lease_target_for(action: dict, tab_id: str) -> str:
    """The lease key for an action.

    DOM tools lease their element target. A WebMCP invocation leases the
    tool handle in the tab (Stage E) -- there is no element target to own.
    Every acquire/release site must use this helper so the keys match.
    """
    tool = action.get("tool", action.get("action", ""))
    if tool == "webmcp_invoke":
        return f"webmcp:{action.get('tool_name', '?')}@{tab_id}"
    return str(action.get("target", action.get("selector",
                   action.get("ref", "?"))))
