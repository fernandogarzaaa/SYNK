"""Stage C regression tests: honest transaction lifecycle, per-action claims,
typed error taxonomy, aggregate transaction states, closed-loop agent,
task-scoped budgets, evidence gating, fail-closed verification, and the
single ExecutionGateway entrypoint.

Run: python -m unittest discover -s tests -v
"""
import time
import unittest
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from harness.world_state import WorldState
from harness.context_manager import ContextManager
from harness.concurrency import OwnershipGraph, LeaseManager, TransactionRunner
from harness.safety import SafetyLayer
from harness.tools import ToolExecutor
from harness.verification.verifier import Verifier
from harness.verification.evidence import (Evidence, ELEMENT_STATE,
                                           BROWSER_EVENT, SCREENSHOT,
                                           satisfies_postcondition)
from harness.verification.results import VERIFIED, UNVERIFIED
from harness.orchestrator import Orchestrator, TaskExecutionContext, AgentLoop
from harness.transactions import (
    TransactionEngine,
    REQUEST, VALIDATED, LEASED, DISPATCHED, ACKNOWLEDGED, OBSERVING,
    FAILED, CONFLICTING,
    TOOL_NOT_FOUND, SCHEMA_INVALID, STALE_REFERENCE, OWNERSHIP_CONFLICT,
    VERIFICATION_FAILED, VERIFICATION_UNAVAILABLE, BROWSER_NOT_READY,
    TX_COMMITTED, TX_PARTIALLY_COMMITTED, TX_FAILED, TX_UNVERIFIED,
)
from harness.gateway import ExecutionGateway


def shop_nodes():
    return [
        {"ref": 1, "role": "textbox", "name": "Email", "tag": "input",
         "selector": "#email", "interactive": True, "index": 0,
         "value": "", "disabled": False},
        {"ref": 2, "role": "button", "name": "Submit", "tag": "button",
         "selector": "#submit", "interactive": True, "index": 1,
         "disabled": False},
    ]


class Harness:
    """Real harness stack (no mocks): engine + verifier + safety + tools."""

    def __init__(self):
        self.world = WorldState()
        self.ownership = OwnershipGraph()
        self.leases = LeaseManager(self.ownership)
        self.safety = SafetyLayer()
        self.ctx = ContextManager()
        self.verifier = Verifier(self.world)
        self.tools = ToolExecutor(self.safety)
        self.engine = TransactionEngine(
            self.world, self.ownership, self.leases, self.tools,
            self.safety, self.ctx, self.verifier)
        # Stage F: the test origin is explicitly trusted (default-deny
        # would otherwise fail every action closed).
        self.engine.policy.register_origin(
            "shop.com", allow=["read", "navigate", "interact", "webmcp"],
            description="stage C test origin")

    def ingest(self, tab_id="t1", url="https://shop.com"):
        view = self.ctx.ingest(url, shop_nodes(), "", tab_id=tab_id)
        self.world.load_full(url, view["nodes"], "Shop", tab_id=tab_id)
        return view


class TestLifecycleStates(unittest.TestCase):
    def test_full_lifecycle_history_order(self):
        h = Harness()
        h.ingest()
        rep = h.engine.execute(
            [{"tool": "type", "ref": 1, "text": "inan@x.com"}],
            task_id="t_task", tab_id="t1", page_url="https://shop.com")
        ex = rep.executions[0]
        states = [s for s, _, _ in ex.history]
        # Honest terminal state: dispatched + acknowledged, never verified
        # without an independent observation.
        self.assertEqual(states[0], "REQUEST")
        self.assertIn("VALIDATED", states)
        self.assertIn("LEASED", states)
        self.assertIn("DISPATCHED", states)
        self.assertIn("ACKNOWLEDGED", states)
        self.assertIn("OBSERVING", states)
        self.assertEqual(ex.state, "UNVERIFIED")
        self.assertEqual(rep.status, TX_UNVERIFIED)
        # Every action gets its own claim.
        self.assertIsNotNone(ex.claim_id)
        self.assertEqual(ex.verification["claim_id"], ex.claim_id)

    def test_batch_expansion_gives_per_action_claims(self):
        h = Harness()
        h.ingest()
        rep = h.engine.execute(
            [{"tool": "bulk", "actions": [
                {"tool": "type", "ref": 1, "text": "inan@x.com"},
                {"tool": "click", "ref": 2}]}],
            task_id="t_task", tab_id="t1", page_url="https://shop.com")
        self.assertEqual(len(rep.executions), 2)
        claim_ids = [e.claim_id for e in rep.executions]
        self.assertEqual(len(set(claim_ids)), 2)  # no shared batch claim
        tools = [e.request.action.get("tool") for e in rep.executions]
        self.assertEqual(tools, ["type", "click"])

    def test_error_taxonomy_distinct_codes(self):
        h = Harness()
        h.ingest()
        # unknown tool
        e = h.engine.execute([{"tool": "teleport", "target": "#x"}],
                             task_id="t", tab_id="t1").executions[0]
        self.assertEqual(e.state, FAILED)
        self.assertEqual(e.error_code, TOOL_NOT_FOUND)
        # schema invalid
        e = h.engine.execute([{"tool": "click"}],
                             task_id="t", tab_id="t1").executions[0]
        self.assertEqual(e.error_code, SCHEMA_INVALID)
        # ownership conflict: human owns the target
        h.ownership.mark_human("#email")
        e = h.engine.execute([{"tool": "type", "ref": 1, "text": "x"}],
                             task_id="t", tab_id="t1").executions[0]
        self.assertEqual(e.state, CONFLICTING)
        self.assertEqual(e.error_code, OWNERSHIP_CONFLICT)

    def test_stale_reference_fails_closed(self):
        h = Harness()
        h.ingest()
        # New snapshot version invalidates the old ref pin.
        h.ctx.ingest("https://shop.com", shop_nodes(), "", tab_id="t1")
        e = h.engine.execute(
            [{"tool": "type", "ref": 1, "ref_version": 1, "text": "x"}],
            task_id="t", tab_id="t1").executions[0]
        self.assertEqual(e.state, CONFLICTING)
        self.assertEqual(e.error_code, STALE_REFERENCE)
        self.assertIn("stale", e.error.lower())


class TestAggregateStatus(unittest.TestCase):
    def _verified_exec(self, h, tool="click", ref=2):
        """Drive one action to VERIFIED via an independent observation."""
        rep = h.engine.execute([{"tool": tool, "ref": ref}],
                               task_id="t_task", tab_id="t1",
                               page_url="https://shop.com")
        ex = rep.executions[0]
        ev = Evidence(
            evidence_id=f"e_{ex.request.action_id}",
            evidence_type=ELEMENT_STATE, source="runtime",
            timestamp=time.time(), action_id=ex.request.action_id,
            task_id="t_task", world_state_version=1,
            payload={"target": "#submit", "visible": True,
                     "provenance_note": "canonical_state_read"},
            provenance="runtime")
        h.verifier.record_evidence(ev)
        result = h.verifier.verify(ex.claim_id)
        return ex, result

    def test_all_verified_commits(self):
        h = Harness()
        h.ingest()
        ex, result = self._verified_exec(h)
        self.assertEqual(result.result, VERIFIED)
        ex.state = "VERIFIED"  # engine-side transition after verification
        self.assertEqual(h.engine._aggregate([ex]), TX_COMMITTED)

    def test_partial_commit(self):
        h = Harness()
        h.ingest()
        ex_ok, _ = self._verified_exec(h)
        ex_ok.state = "VERIFIED"
        ex_bad = h.engine.execute([{"tool": "teleport"}],
                                  task_id="t", tab_id="t1").executions[0]
        self.assertEqual(ex_bad.state, FAILED)
        self.assertEqual(h.engine._aggregate([ex_ok, ex_bad]),
                         TX_PARTIALLY_COMMITTED)

    def test_all_failed(self):
        h = Harness()
        h.ingest()
        exs = h.engine.execute([{"tool": "teleport"}, {"tool": "click"}],
                               task_id="t", tab_id="t1").executions
        self.assertEqual(h.engine._aggregate(exs), TX_FAILED)

    def test_dispatched_but_unobserved_is_unverified(self):
        h = Harness()
        h.ingest()
        rep = h.engine.execute([{"tool": "click", "ref": 2}],
                               task_id="t", tab_id="t1")
        self.assertEqual(rep.executions[0].state, "UNVERIFIED")
        self.assertEqual(rep.status, TX_UNVERIFIED)


class TestEvidenceGating(unittest.TestCase):
    def test_command_accepted_cannot_verify(self):
        h = Harness()
        h.ingest()
        rep = h.engine.execute([{"tool": "type", "ref": 1, "text": "inan@x.com"}],
                               task_id="t_task", tab_id="t1",
                               page_url="https://shop.com")
        ex = rep.executions[0]
        # The engine recorded dispatch evidence (BROWSER_EVENT: the executor
        # accepted the command); the verifier must still refuse because
        # acceptance is not observation.
        types = {e.evidence_type
                 for e in h.verifier.evidence_store.values()}
        self.assertIn(BROWSER_EVENT, types)
        self.assertEqual(ex.verification["result"], UNVERIFIED)

    def test_unknown_verification_check_fails_closed(self):
        h = Harness()
        h.ingest()
        rep = h.engine.execute(
            [{"tool": "click", "ref": 2,
              "verification": ["does_the_impossible"]}],
            task_id="t", tab_id="t1", page_url="https://shop.com")
        ex = rep.executions[0]
        self.assertEqual(ex.state, "UNVERIFIED")
        self.assertEqual(ex.error_code, VERIFICATION_UNAVAILABLE)

    def test_contradicted_check_fails(self):
        h = Harness()
        h.ingest()
        rep = h.engine.execute(
            [{"tool": "navigate", "url": "https://shop.com/checkout",
              "verification": ["url_is"]}],
            task_id="t", tab_id="t1", page_url="https://shop.com")
        ex = rep.executions[0]
        # url_is against the pre-navigation URL must contradict.
        self.assertEqual(ex.error_code, VERIFICATION_FAILED)

    def test_screenshot_without_vision_cannot_verify(self):
        from harness.verification.evidence import satisfies_postcondition
        ev = Evidence(
            evidence_id="e_shot", evidence_type=SCREENSHOT,
            source="runtime", timestamp=time.time(),
            action_id="a1", task_id="t",
            payload={"vision_verified": False})
        self.assertFalse(
            satisfies_postcondition(SCREENSHOT, "element_value",
                                    ev.payload))


class TestClosedLoop(unittest.TestCase):
    def _loop_harness(self):
        h = Harness()
        loop_state = type("S", (), {})()
        loop_state.world = h.world
        loop_state.ctx = h.ctx
        loop_state.bus = type("B", (), {"seq": 0,
                                       "recent": lambda self, n: []})()
        loop_state.ownership = h.ownership
        loop_state.leases = h.leases
        loop_state.safety = h.safety
        loop_state.mem = type("M", (), {
            "summary_for_prompt": lambda self: ""})()
        loop_state.verifier = h.verifier
        loop_state.engine = h.engine
        loop_state.ladder = type(
            "L", (), {"choose_level":
                      lambda self, d, g, vision_available=False:
                      (None, "", None)})()
        loop = AgentLoop(loop_state, Orchestrator())
        return h, loop

    def test_replan_then_verify(self):
        h, loop = self._loop_harness()
        h.ingest(tab_id="t_loop")
        ctx = loop.begin("fill the form", {"tab_id": "t_loop"})
        flags = loop.observe(ctx, {"url": "https://shop.com"})
        d = loop.next_action(ctx, flags)
        self.assertEqual(d["decision"], "act")
        aid, claim = d["action_id"], d["claim_id"]
        # First attempt: browser not ready -> replan, lease released.
        r = loop.report(ctx, aid, claim, "not_executed",
                        error_code=BROWSER_NOT_READY, reason="tab busy")
        self.assertEqual(r["decision"], "replan")
        self.assertEqual(len(h.leases.leases), 0)
        # Second attempt: browser executes, observation confirms value.
        d2 = loop.next_action(ctx, loop.observe(ctx,
                                                {"url": "https://shop.com"}))
        self.assertEqual(d2["decision"], "act")
        aid2 = d2["action_id"]
        h.world.apply_event({"type": "value.changed",
                             "data": {"tab_id": "t_loop",
                                      "target": "#email",
                                      "value": d2["action"]["text"],
                                      "task_id": ctx.task_id,
                                      "action_id": aid2}})
        r2 = loop.report(ctx, aid2, d2["claim_id"], "executed")
        self.assertEqual(r2["decision"], "continue")
        self.assertEqual(r2["verification"]["result"], VERIFIED)
        self.assertEqual(ctx.verified, 1)

    def test_stale_ref_triggers_replan(self):
        h, loop = self._loop_harness()
        h.ingest(tab_id="t_sr")
        ctx = loop.begin("fill the form", {"tab_id": "t_sr"})
        d = loop.next_action(ctx, loop.observe(ctx,
                                               {"url": "https://shop.com"}))
        self.assertEqual(d["decision"], "act")
        # The prepared action pinned ref_version=1. A new snapshot (v2)
        # makes that pin stale; re-preparing the same pinned action must
        # fail closed, and the loop maps it to a replan.
        h.ctx.ingest("https://shop.com", shop_nodes(), "", tab_id="t_sr")
        pinned = dict(d["action"])
        ex = h.engine.prepare(pinned, task_id=ctx.task_id, tab_id="t_sr",
                              page_url="https://shop.com")
        self.assertEqual(ex.error_code, STALE_REFERENCE)
        decision = loop._map_prepare_failure(ctx, ex)
        self.assertEqual(decision["decision"], "replan")
        self.assertIn(STALE_REFERENCE, decision["reason"])

    def test_repeated_failures_ask_human(self):
        h, loop = self._loop_harness()
        h.ingest(tab_id="t_rf")
        ctx = loop.begin("fill the form", {"tab_id": "t_rf"})
        decisions = []
        for _ in range(8):
            d = loop.next_action(ctx, loop.observe(ctx,
                                                   {"url": "https://shop.com"}))
            decisions.append(d["decision"])
            if d["decision"] != "act":
                break
            r = loop.report(ctx, d["action_id"], d["claim_id"],
                            "not_executed", error_code=BROWSER_NOT_READY,
                            reason="tab busy")
            decisions.append(r["decision"])
            if r["decision"] != "replan":
                break
        # Circuit breaker: repeated contention escalates to the human, and
        # the task stops running (later next_action calls abort cleanly).
        self.assertIn("request-human", decisions)
        self.assertEqual(ctx.status, "waiting_human")
        d = loop.next_action(ctx, loop.observe(ctx,
                                               {"url": "https://shop.com"}))
        self.assertEqual(d["decision"], "abort")

    def test_isolated_task_budgets(self):
        _, loop = self._loop_harness()
        c1 = TaskExecutionContext(task_id="t_a", goal="g", max_steps=1)
        c2 = TaskExecutionContext(task_id="t_b", goal="g", max_steps=50)
        orch = Orchestrator()
        # Exhaust task A's budget.
        orch.plan_for_context(c1, "fill the form", "ctx")
        p = orch.plan_for_context(c1, "fill the form", "ctx")
        self.assertTrue(p.get("budget_exhausted"))
        # Task B is unaffected: its own counter still allows planning.
        p2 = orch.plan_for_context(c2, "fill the form", "ctx")
        self.assertFalse(p2.get("budget_exhausted"))
        self.assertEqual(c2.steps_used, 1)
        self.assertEqual(c1.steps_used, 2)


class TestGateway(unittest.TestCase):
    def test_single_entrypoint_shape(self):
        h = Harness()
        h.ingest()
        gw = ExecutionGateway(h.engine, mem=None)
        out = gw.execute({
            "actions": [{"tool": "type", "ref": 1, "text": "inan@x.com"}],
            "task_id": "t_gw", "tab_id": "t1",
            "page_url": "https://shop.com"})
        self.assertTrue(out["ok"])
        self.assertEqual(out["task_id"], "t_gw")
        self.assertIn("transaction_id", out)
        self.assertEqual(out["transaction_status"], TX_UNVERIFIED)
        self.assertEqual(len(out["action_results"]), 1)
        self.assertEqual(len(out["verifications"]), 1)
        ar = out["action_results"][0]
        self.assertEqual(ar["state"], "UNVERIFIED")
        self.assertEqual(out["verifications"][0]["claim_id"],
                         ar["claim_id"])

    def test_legacy_transaction_runner_still_reports_executed(self):
        # Benchmarks / Stage B callers depend on the legacy verdict.
        world = WorldState()
        ownership = OwnershipGraph()
        leases = LeaseManager(ownership)

        class T:
            def run(self, action, page_url="", user_consented=False,
                    tab_id="default"):
                return {"ok": True, "command": action.get("tool")}

        tx = TransactionRunner(world, ownership, leases, T(), SafetyLayer())
        # Stage F: default-deny policy requires the test origin registered.
        tx.engine.policy.register_origin(
            "x.com", allow=["read", "navigate", "interact"],
            description="stage C test origin")
        r = tx.run({"tool": "click", "target": "#b", "tab_id": "t9"},
                   "https://x.com", tab_id="t9")
        self.assertEqual(r["verdict"], "executed")
        self.assertTrue(r["ok"])


class TestPageLoadedDedup(unittest.TestCase):
    def test_single_journal_entry_per_snapshot(self):
        from harness.server import State
        st = State(":memory:")
        # Simulate exactly what Handler._snapshot does after the Stage C fix:
        # ONE bus event carrying the full snapshot payload (no direct
        # world.load_full() call).
        st.bus.emit("page.loaded", {"url": "https://x.com/", "title": "X",
                                    "nodes": [], "full": True,
                                    "tab_id": "default"})
        entries = [e["type"] for e in st.world.journal.events()
                   if e["type"] == "page.loaded"]
        self.assertEqual(len(entries), 1)


if __name__ == "__main__":
    unittest.main()
