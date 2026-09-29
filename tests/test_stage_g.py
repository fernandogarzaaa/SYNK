"""Stage G regression tests: honest scheduler, workflow learning, model
router, task compiler.

Run: python -m unittest discover -s tests -v
"""
import json
import os
import time
import unittest
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from harness.world_state import WorldState
from harness.context_manager import ContextManager
from harness.concurrency import OwnershipGraph, LeaseManager
from harness.safety import SafetyLayer
from harness.tools import ToolExecutor
from harness.verification.verifier import Verifier
from harness.verification.evidence import Evidence, ELEMENT_STATE
from harness.verification.results import VERIFIED
from harness.transactions import TransactionEngine
from harness.transactions import OWNERSHIP_CONFLICT as _OWNERSHIP_CONFLICT
from harness.transactions import CONFLICTING as _CONFLICTING
from harness.policy import OriginPolicyRegistry

from harness.compiler import (compile_spec, CompileError, SCHEMA_INVALID,
                              UNKNOWN_TOOL, MISSING_PARAM, BAD_PRECONDITION,
                              UNKNOWN_VERIFICATION, COMPILE_POLICY_DENIED,
                              compile_intent, ir_to_actions)
from harness.task_scheduler import TaskScheduler, EXECUTION_MODE
from harness.workflow_store import WorkflowLearner
from harness.model_router import ModelRegistry, ModelRouter


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
    """Real engine stack (no mocks), as in Stage C tests."""

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
        self.engine.policy.register_origin(
            "shop.com", allow=["read", "navigate", "interact", "webmcp"],
            description="stage G test origin")

    def ingest(self, tab_id="t1", url="https://shop.com"):
        view = self.ctx.ingest(url, shop_nodes(), "", tab_id=tab_id)
        self.world.load_full(url, view["nodes"], "Shop", tab_id=tab_id)
        return view


# ---------------------------------------------------------------- compiler

class TestCompiler(unittest.TestCase):
    def _policy(self):
        p = OriginPolicyRegistry()
        p.register_origin("shop.com", allow=["read", "navigate", "interact"],
                          description="t")
        return p

    def test_fill_form_compiles_and_validates(self):
        plan = compile_spec({
            "task": "fill_form", "origin": "shop.com",
            "page_url": "https://shop.com/login",
            "fields": [{"ref": 1, "value": "inan@x.com"}],
            "submit_ref": 2,
        }, policy=self._policy())
        self.assertTrue(plan["ok"])
        self.assertTrue(plan["policy_checked"])
        self.assertEqual(plan["action_count"], 2)
        self.assertEqual(plan["actions"][0]["tool"], "type")
        self.assertEqual(plan["actions"][0]["text"], "inan@x.com")
        self.assertEqual(plan["actions"][1]["tool"], "click")

    def test_unknown_origin_denied(self):
        with self.assertRaises(CompileError) as cm:
            compile_spec({"task": "navigate",
                          "origin": "evil.com",
                          "url": "https://evil.com"},
                         policy=self._policy())
        self.assertEqual(cm.exception.code, COMPILE_POLICY_DENIED)

    def test_missing_origin_with_policy_rejected(self):
        with self.assertRaises(CompileError) as cm:
            compile_spec({"task": "extract"}, policy=self._policy())
        self.assertEqual(cm.exception.code, SCHEMA_INVALID)

    def test_unknown_tool(self):
        with self.assertRaises(CompileError) as cm:
            compile_spec({"task": "raw", "origin": "shop.com",
                          "actions": [{"tool": "teleport"}]},
                         policy=self._policy())
        self.assertEqual(cm.exception.code, UNKNOWN_TOOL)

    def test_missing_param(self):
        with self.assertRaises(CompileError) as cm:
            compile_spec({"task": "raw", "origin": "shop.com",
                          "actions": [{"tool": "type", "ref": 1}]},
                         policy=self._policy())
        self.assertEqual(cm.exception.code, MISSING_PARAM)

    def test_bad_precondition(self):
        with self.assertRaises(CompileError) as cm:
            compile_spec({"task": "raw", "origin": "shop.com",
                          "actions": [{"tool": "snapshot",
                                       "preconditions": ["not a condition"]}]},
                         policy=self._policy())
        self.assertEqual(cm.exception.code, BAD_PRECONDITION)

    def test_unknown_verification_fails_closed(self):
        with self.assertRaises(CompileError) as cm:
            compile_spec({"task": "raw", "origin": "shop.com",
                          "actions": [{"tool": "snapshot",
                                       "verification": ["vibes"]}]},
                         policy=self._policy())
        self.assertEqual(cm.exception.code, UNKNOWN_VERIFICATION)

    def test_free_text_rejected(self):
        with self.assertRaises(CompileError) as cm:
            compile_spec("click the submit button", policy=self._policy())
        self.assertEqual(cm.exception.code, SCHEMA_INVALID)

    def test_denied_action_class_rejected(self):
        p = OriginPolicyRegistry()
        p.register_origin("shop.com", allow=["read"], description="t")
        with self.assertRaises(CompileError) as cm:
            compile_spec({"task": "raw", "origin": "shop.com",
                          "actions": [{"tool": "click", "ref": 2}]},
                         policy=p)
        self.assertEqual(cm.exception.code, COMPILE_POLICY_DENIED)

    def test_no_policy_flags_unchecked(self):
        plan = compile_spec({"task": "extract"})
        self.assertTrue(plan["ok"])
        self.assertFalse(plan["policy_checked"])

    def test_legacy_compile_intent_still_works(self):
        ir = compile_intent("fill_form", {
            "fields": [(1, "inan@x.com")], "submit_ref": 2})
        self.assertFalse(ir["policy_checked"])
        actions = ir_to_actions(ir)
        self.assertEqual(len(actions), 2)
        self.assertEqual(actions[0]["tool"], "type")


# ---------------------------------------------------------------- scheduler

class TestScheduler(unittest.TestCase):
    def _sched(self):
        h = Harness()
        return TaskScheduler(h.leases), h

    def _run_ok(self, order):
        def exec_fn(task):
            order.append(task["task_id"])
            return {"ok": True, "task_id": task["task_id"]}
        return exec_fn

    def test_mode_is_sequential_only(self):
        self.assertEqual(EXECUTION_MODE, "SEQUENTIAL")

    def test_sequential_dispatch_order(self):
        sched, _ = self._sched()
        order = []
        sched.submit({"goal": "first", "tab_id": "t1",
                      "actions": [{"tool": "snapshot"}]})
        sched.submit({"goal": "second", "tab_id": "t1",
                      "actions": [{"tool": "snapshot"}]})
        r1 = sched.run_next(self._run_ok(order))
        r2 = sched.run_next(self._run_ok(order))
        self.assertEqual(r1["status"], "completed")
        self.assertEqual(r2["status"], "completed")
        self.assertEqual(len(order), 2)
        self.assertIsNone(sched.run_next(self._run_ok(order)))

    def test_cancellation(self):
        sched, _ = self._sched()
        t = sched.submit({"goal": "cancel me", "tab_id": "t1",
                          "actions": [{"tool": "snapshot"}]})
        self.assertTrue(sched.cancel(t.task_id))
        self.assertEqual(sched.status(t.task_id)["status"], "cancelled")
        order = []
        self.assertIsNone(sched.run_next(self._run_ok(order)))
        self.assertEqual(order, [])

    def test_cancel_unknown_or_terminal(self):
        sched, _ = self._sched()
        self.assertFalse(sched.cancel("nope"))

    def test_lease_aware_dispatch_blocks_then_runs(self):
        sched, h = self._sched()
        t = sched.submit({"goal": "lease gated", "tab_id": "t9",
                          "actions": [{"tool": "snapshot"}]})
        blocker = h.leases.acquire("tab:t9", intent="blocker")
        self.assertIsNotNone(blocker)
        r = sched.run_next(self._run_ok([]))
        self.assertEqual(r["status"], "blocked")
        self.assertIn("lease", r["block_reason"])
        h.leases.release("tab:t9", blocker["lease"])
        order = []
        r2 = sched.run_next(self._run_ok(order))
        self.assertEqual(r2["status"], "completed")
        self.assertEqual(order, [t.task_id])

    def test_same_task_nests_engine_lease_under_tab_lease(self):
        h = Harness()
        h.ingest()
        holder = h.leases.acquire("tab:t1", intent="task:A", task_id="A")
        self.assertIsNotNone(holder)
        rep = h.engine.execute(
            [{"tool": "click", "ref": 2, "target": "#submit"}],
            task_id="A", tab_id="t1", page_url="https://shop.com")
        ex = rep.executions[0]
        self.assertNotEqual(ex.error_code, _OWNERSHIP_CONFLICT)
        h.leases.release("tab:t1", holder["lease"])

    def test_foreign_task_cannot_nest_under_tab_lease(self):
        h = Harness()
        h.ingest()
        holder = h.leases.acquire("tab:t1", intent="task:A", task_id="A")
        self.assertIsNotNone(holder)
        rep = h.engine.execute(
            [{"tool": "click", "ref": 2, "target": "#submit"}],
            task_id="B", tab_id="t1", page_url="https://shop.com")
        ex = rep.executions[0]
        self.assertEqual(ex.state, _CONFLICTING)
        self.assertEqual(ex.error_code, _OWNERSHIP_CONFLICT)
        h.leases.release("tab:t1", holder["lease"])

    def test_human_owned_tab_blocks_even_same_task(self):
        h = Harness()
        h.ingest()
        h.leases.acquire("tab:t1", intent="task:A", task_id="A")
        h.ownership.acquire_human("tab:t1")
        rep = h.engine.execute(
            [{"tool": "click", "ref": 2, "target": "#submit"}],
            task_id="A", tab_id="t1", page_url="https://shop.com")
        ex = rep.executions[0]
        self.assertEqual(ex.error_code, _OWNERSHIP_CONFLICT)

    def test_scheduler_dispatch_end_to_end_through_engine(self):
        h = Harness()
        h.ingest()
        sched = TaskScheduler(h.leases)
        t = sched.submit({"goal": "nested dispatch", "tab_id": "t1",
                          "actions": [{"tool": "click", "ref": 2,
                                       "target": "#submit"}]})
        states = []

        def exec_fn(task):
            rep = h.engine.execute(task["actions"], task_id=task["task_id"],
                                   tab_id=task["tab_id"],
                                   page_url="https://shop.com")
            states.extend(e.state for e in rep.executions)
            return {"ok": True}

        r = sched.run_next(exec_fn)
        self.assertEqual(r["status"], "completed")
        self.assertNotIn(_CONFLICTING, states)
        # tab lease released after the task finished
        self.assertEqual(h.leases.ownership.state_of("tab:t1"), "FREE")

    def test_step_budget_rejected_at_submit(self):
        sched, _ = self._sched()
        with self.assertRaises(ValueError):
            sched.submit({"goal": "too big", "tab_id": "t1",
                          "actions": [{"tool": "snapshot"}] * 5,
                          "budgets": {"max_steps": 2}})

    def test_time_budget_enforced(self):
        sched, _ = self._sched()
        t = sched.submit({"goal": "slow", "tab_id": "t1",
                          "actions": [{"tool": "snapshot"}],
                          "budgets": {"max_seconds": 0.01}})
        def slow(task):
            time.sleep(0.05)
            return {"ok": True}
        r = sched.run_next(slow)
        self.assertEqual(r["status"], "failed")
        self.assertEqual(r["result"]["error"], "BUDGET_EXCEEDED")

    def test_dependencies_gate_dispatch(self):
        sched, _ = self._sched()
        a = sched.submit({"goal": "a", "tab_id": "t1",
                          "actions": [{"tool": "snapshot"}]})
        b = sched.submit({"goal": "b", "tab_id": "t1",
                          "depends_on": [a.task_id],
                          "actions": [{"tool": "snapshot"}]})
        order = []
        # b is not ready until a completes; only a dispatches
        r = sched.run_next(self._run_ok(order))
        self.assertEqual(r["task_id"], a.task_id)
        r = sched.run_next(self._run_ok(order))
        self.assertEqual(r["task_id"], b.task_id)

    def test_unknown_dependency_rejected(self):
        sched, _ = self._sched()
        with self.assertRaises(ValueError):
            sched.submit({"goal": "x", "depends_on": ["ghost"]})

    def test_compile_at_submit_with_policy(self):
        sched, h = self._sched()
        t = sched.submit({
            "goal": "fill login", "tab_id": "t1",
            "spec": {"task": "fill_form", "origin": "shop.com",
                     "fields": [{"ref": 1, "value": "a@b.c"}],
                     "submit_ref": 2},
        }, policy=h.engine.policy)
        self.assertEqual(len(t.actions), 2)
        order = []
        r = sched.run_next(self._run_ok(order))
        self.assertEqual(r["status"], "completed")

    def test_compile_failure_at_submit(self):
        sched, h = self._sched()
        with self.assertRaises(CompileError):
            sched.submit({
                "goal": "evil", "tab_id": "t1",
                "spec": {"task": "navigate", "origin": "evil.com",
                         "url": "https://evil.com"},
            }, policy=h.engine.policy)

    def test_routing_decision_recorded(self):
        sched, _ = self._sched()
        os.environ["SYNK_MODELS"] = json.dumps([
            {"name": "tiny", "capabilities": ["text"], "cost_tier": "free"},
            {"name": "seer", "capabilities": ["text", "vision"],
             "cost_tier": "cheap"},
        ])
        try:
            router = ModelRouter(ModelRegistry.from_env())
            t = sched.submit({"goal": "look at chart", "tab_id": "t1",
                              "actions": [{"tool": "snapshot"}],
                              "requirements": {"needs_vision": True}},
                             router=router)
        finally:
            del os.environ["SYNK_MODELS"]
        self.assertEqual(t.routing["selected"], "seer")
        self.assertTrue(t.routing["ok"])


# ---------------------------------------------------------------- workflows

class FakeReport:
    """Minimal TransactionReport shape: executions with request.action +
    verification dicts. (Unit-test stand-in, not presented as engine output.)"""

    class Ex:
        def __init__(self, action, verified):
            self.request = type("R", (), {"action": action})()
            self.verification = {"result": "VERIFIED" if verified
                                 else "UNVERIFIED"}

    def __init__(self, actions, verified=True):
        self.executions = [self.Ex(a, verified) for a in actions]


class TestWorkflowLearning(unittest.TestCase):
    def _actions(self):
        return [
            {"tool": "type", "ref": 1, "target": "#email",
             "text": "inan@x.com"},
            {"tool": "click", "ref": 2, "target": "#submit"},
        ]

    def test_learn_requires_verified(self):
        wl = WorkflowLearner()
        r = wl.learn_from_report(FakeReport(self._actions(), verified=False),
                                 goal="log in", domain="shop.com",
                                 task_id="t1")
        self.assertFalse(r["learned"])
        self.assertEqual(wl.list(), [])

    def test_learn_parameterizes_and_reobserves(self):
        wl = WorkflowLearner()
        r1 = wl.learn_from_report(FakeReport(self._actions()), goal="log in",
                                  domain="shop.com", task_id="t1")
        self.assertTrue(r1["learned"])
        w = wl.get(r1["name"])
        self.assertEqual(w["success_count"], 1)
        # literal values became params with recorded defaults
        self.assertIn("{{p0}}", json.dumps(w["steps"]))
        self.assertEqual(w["defaults"]["p0"], "inan@x.com")
        # same shape again -> bump, not duplicate
        r2 = wl.learn_from_report(
            FakeReport([{"tool": "type", "ref": 1, "target": "#email",
                         "text": "other@y.z"},
                        {"tool": "click", "ref": 2, "target": "#submit"}]),
            goal="log in", domain="shop.com", task_id="t2")
        self.assertEqual(r1["name"], r2["name"])
        self.assertEqual(wl.get(r1["name"])["success_count"], 2)
        self.assertEqual(len(wl.list()), 1)

    def test_suggest_matches_with_rationale(self):
        wl = WorkflowLearner()
        wl.learn_from_report(FakeReport(self._actions()), goal="log in to shop",
                             domain="shop.com", task_id="t1")
        sugg = wl.suggest("how do I log in to the shop?")
        self.assertTrue(sugg)
        top = sugg[0]
        self.assertIn("rationale", top)
        self.assertTrue(any("matched" in r for r in top["rationale"]))
        self.assertGreater(top["score"], 0)

    def test_suggest_no_match(self):
        wl = WorkflowLearner()
        self.assertEqual(wl.suggest("launch the rockets"), [])

    def test_replay_renders_params_and_uses_executor(self):
        wl = WorkflowLearner()
        r = wl.learn_from_report(FakeReport(self._actions()), goal="log in",
                                 domain="shop.com", task_id="t1")
        seen = []

        def executor(actions, **identity):
            seen.extend(actions)
            seen.append(("_identity", identity))
            return {"ok": True, "transaction_status": "COMMITTED"}

        out = wl.replay(r["name"], {"p0": "new@x.com"}, executor,
                        tab_id="t1")
        self.assertTrue(out["ok"])
        self.assertEqual(seen[0]["text"], "new@x.com")
        self.assertEqual(seen[1]["tool"], "click")
        self.assertEqual(seen[2], ("_identity", {"tab_id": "t1"}))

    def test_replay_defaults_when_no_params(self):
        wl = WorkflowLearner()
        r = wl.learn_from_report(FakeReport(self._actions()), goal="log in",
                                 domain="shop.com", task_id="t1")
        out = wl.replay(r["name"], None,
                        lambda actions: {"ok": True})
        self.assertEqual(out["rendered_actions"][0]["text"], "inan@x.com")

    def test_replay_unknown_workflow(self):
        wl = WorkflowLearner()
        with self.assertRaises(KeyError):
            wl.replay("wf_nope", {}, lambda a: {"ok": True})

    def test_learn_from_real_engine_report(self):
        """End to end: real transaction engine -> verified -> learned."""
        h = Harness()
        h.ingest()
        rep = h.engine.execute([{"tool": "click", "ref": 2}],
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
        self.assertEqual(result.result, VERIFIED)
        ex.state = "VERIFIED"
        ex.verification = result.to_dict()
        wl = WorkflowLearner()
        learned = wl.learn_from_report(rep, goal="submit the form",
                                       domain="shop.com", task_id="t_task")
        self.assertTrue(learned["learned"])
        self.assertEqual(wl.get(learned["name"])["success_count"], 1)


# ---------------------------------------------------------------- router

class TestModelRouter(unittest.TestCase):
    def _two(self):
        return ModelRegistry([
            {"name": "tiny", "provider": "local",
             "capabilities": ["text"], "context_tokens": 8192,
             "cost_tier": "free"},
            {"name": "seer", "provider": "cloud",
             "capabilities": ["text", "vision", "long_context"],
             "context_tokens": 128000, "cost_tier": "expensive"},
        ], source="test")

    def test_vision_requirement_selects_vision_backend(self):
        r = ModelRouter(self._two())
        d = r.route({"needs_vision": True}, task_id="t1")
        self.assertTrue(d["ok"])
        self.assertEqual(d["selected"], "seer")
        self.assertTrue(any("vision" in x for x in d["rationale"]))

    def test_cheap_text_task_picks_cheapest(self):
        r = ModelRouter(self._two())
        d = r.route({}, task_id="t1")
        self.assertEqual(d["selected"], "tiny")

    def test_long_horizon_prefers_context(self):
        r = ModelRouter(self._two())
        d = r.route({"long_horizon": True}, task_id="t1")
        self.assertEqual(d["selected"], "seer")

    def test_deterministic(self):
        r = ModelRouter(self._two())
        a = r.route({"needs_vision": True}, task_id="t1")
        b = r.route({"needs_vision": True}, task_id="t1")
        self.assertEqual(a["selected"], b["selected"])
        self.assertEqual(a["rationale"], b["rationale"])

    def test_single_backend_honesty(self):
        r = ModelRouter(ModelRegistry(
            [{"name": "solo", "capabilities": ["text"]}], source="test"))
        d = r.route({"needs_vision": False})
        self.assertTrue(d["single_backend"])
        self.assertEqual(d["selected"], "solo")
        self.assertTrue(any("single backend" in x for x in d["rationale"]))

    def test_no_eligible_backend_refuses(self):
        r = ModelRouter(ModelRegistry(
            [{"name": "solo", "capabilities": ["text"]}], source="test"))
        d = r.route({"needs_vision": True})
        self.assertFalse(d["ok"])
        self.assertIsNone(d["selected"])
        self.assertTrue(any("vision" in x for x in d["candidates"][0]
                            ["missing"]))

    def test_registry_from_env(self):
        os.environ["SYNK_MODELS"] = json.dumps([
            {"name": "a", "capabilities": ["text", "vision"],
             "cost_tier": "cheap"},
        ])
        try:
            reg = ModelRegistry.from_env()
        finally:
            del os.environ["SYNK_MODELS"]
        self.assertEqual(reg.source, "env: SYNK_MODELS")
        self.assertEqual(reg.entries[0]["name"], "a")

    def test_registry_default_is_single_builtin(self):
        env = {k: v for k, v in os.environ.items()
               if k not in ("SYNK_MODELS", "SYNK_MODELS_FILE")}
        old = dict(os.environ)
        os.environ.clear()
        os.environ.update(env)
        try:
            reg = ModelRegistry.from_env()
        finally:
            os.environ.clear()
            os.environ.update(old)
        self.assertEqual(len(reg.entries), 1)
        d = ModelRouter(reg).route({})
        self.assertTrue(d["single_backend"])


if __name__ == "__main__":
    unittest.main()
