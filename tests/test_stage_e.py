"""Stage E: real WebMCP through per-page model-context discovery.

Covers: fake model-context discovery, missing-tool fail-closed (zero
fallback calls), cross-tab handle rejection, stale handles after
navigation, deterministic selection with full rationale, ExecutionGateway
integration (claims + evidence + verification), the PARTIAL fixture
fallback never verifying a claim, and model-context-present-with-zero-
tools never enabling the fallback.

The FakeModelContextTransport is an explicit TEST DOUBLE: nothing about
its output is presented as a live page. Live-browser WebMCP
(navigator.modelContext in a real page) is NOT verified here -- no live
browser is available in this environment.
"""
import unittest

from harness.session import SessionManager
from harness.webmcp.gateway import WebMCPGateway
from harness.webmcp.scope import scope_for_tab
from harness.webmcp.transport import FakeModelContextTransport
from harness.webmcp.selection import CapabilitySelector


TOOLS = [
    {"name": "searchProducts",
     "description": "search the store catalog for products matching a query",
     "input_schema": {"type": "object",
                      "properties": {"query": {"type": "string"}},
                      "required": ["query"]},
     "risk": "low"},
    {"name": "addToCart",
     "description": "add a product to the shopping cart by id",
     "input_schema": {"type": "object",
                      "properties": {"product_id": {"type": "string"}},
                      "required": ["product_id"]},
     "risk": "medium"},
]


def make_sessions(tab_id="t1", url="https://shop.example/"):
    sm = SessionManager()
    sm.register_frame(tab_id, "main", url=url)
    return sm


def make_gateway(sessions, transport, **kw):
    return WebMCPGateway(transport=transport, sessions=sessions, **kw)


class TestFakeDiscovery(unittest.TestCase):
    def test_discover_advertised_tools(self):
        sm = make_sessions()
        gw = make_gateway(sm, FakeModelContextTransport(tools=TOOLS))
        scope = scope_for_tab(sm, "t1", "main")
        self.assertIsNotNone(scope)
        res = gw.discover(scope)
        self.assertTrue(res["available"])
        self.assertEqual([t["name"] for t in res["tools"]],
                         ["searchProducts", "addToCart"])

    def test_discover_unavailable_when_no_context(self):
        sm = make_sessions()
        gw = make_gateway(
            sm, FakeModelContextTransport(tools=[], available=False,
                                          reason="webmcp_unavailable"))
        scope = scope_for_tab(sm, "t1", "main")
        res = gw.discover(scope)
        self.assertFalse(res["available"])
        self.assertEqual(res["tools"], [])

    def test_discover_present_with_zero_tools_is_not_unavailable(self):
        # A page CAN expose a model context with zero tools. That is
        # distinct from "no model context" and must never enable the
        # fixture fallback.
        sm = make_sessions()
        gw = make_gateway(
            sm, FakeModelContextTransport(tools=[], available=True))
        scope = scope_for_tab(sm, "t1", "main")
        res = gw.discover(scope)
        self.assertTrue(res["available"])
        self.assertEqual(res["tools"], [])


class TestFailClosed(unittest.TestCase):
    def test_unadvertised_tool_fails_with_zero_fallback_calls(self):
        sm = make_sessions()
        tr = FakeModelContextTransport(tools=TOOLS)
        gw = make_gateway(sm, tr, allow_fallback=True)  # fallback ON
        scope = scope_for_tab(sm, "t1", "main")
        inv = gw.invoke("deleteAccount", {}, scope)
        self.assertFalse(inv.ok)
        self.assertIn("WEBMCP_TOOL_NOT_ADVERTISED", inv.result.error)
        self.assertEqual(tr.invoke_calls, [])
        self.assertEqual(inv.error_code, "TOOL_NOT_FOUND")
        self.assertFalse(inv.partial)

    def test_zero_tools_never_enables_fallback(self):
        sm = make_sessions()
        tr = FakeModelContextTransport(tools=[], available=True)
        gw = make_gateway(sm, tr, allow_fallback=True)
        scope = scope_for_tab(sm, "t1", "main")
        inv = gw.invoke("searchProducts", {"query": "x"}, scope)
        self.assertFalse(inv.ok)
        self.assertIn("WEBMCP_TOOL_NOT_ADVERTISED", inv.result.error)
        self.assertFalse(inv.partial)
        self.assertEqual(tr.invoke_calls, [])

    def test_unavailable_without_opt_in_fails_closed(self):
        sm = make_sessions()
        tr = FakeModelContextTransport(tools=[], available=False)
        gw = make_gateway(sm, tr, allow_fallback=False)
        scope = scope_for_tab(sm, "t1", "main")
        inv = gw.invoke("searchProducts", {"query": "x"}, scope)
        self.assertFalse(inv.ok)
        self.assertIn("WEBMCP_UNAVAILABLE", inv.result.error)
        self.assertFalse(inv.partial)

    def test_unknown_tab_fails_closed(self):
        sm = make_sessions()
        gw = make_gateway(sm, FakeModelContextTransport(tools=TOOLS))
        scope = scope_for_tab(sm, "nope", "main")
        self.assertIsNone(scope)
        inv = gw.invoke("searchProducts", {}, scope)
        self.assertFalse(inv.ok)
        self.assertIn("WEBMCP_SCOPE_VIOLATION", inv.result.error)


class TestHandleScoping(unittest.TestCase):
    def test_handle_ids_are_scope_bound(self):
        # Handle ids are DERIVED from the scope, never presented by the
        # caller: a handle minted for tab A is unaddressable from tab B.
        sm = make_sessions("t1")
        sm.register_frame("t2", "main", url="https://other.example/")
        s1 = scope_for_tab(sm, "t1", "main")
        s2 = scope_for_tab(sm, "t2", "main")
        self.assertNotEqual(s1.handle_id_for("searchProducts"),
                            s2.handle_id_for("searchProducts"))
        # ... and stable for the same scope (no hash() randomness).
        self.assertEqual(s1.handle_id_for("searchProducts"),
                         s1.handle_id_for("searchProducts"))

    def test_cross_tab_invoke_fails_closed(self):
        from harness.webmcp.transport import WebMCPDiscoveryResult

        class ScopedFake(FakeModelContextTransport):
            """Advertises tools on t1 only; t2 reports no model context."""

            def discover(self, scope):
                if scope.tab_id != "t1":
                    return WebMCPDiscoveryResult(
                        available=False, reason="webmcp_unavailable",
                        scope_key=scope.key())
                return super().discover(scope)

        sm = make_sessions("t1")
        sm.register_frame("t2", "main", url="https://other.example/")
        tr = ScopedFake(
            tools=TOOLS, results={"searchProducts": {"ok": True,
                                                     "result": {"n": 1}}})
        gw = make_gateway(sm, tr)
        s1 = scope_for_tab(sm, "t1", "main")
        gw.discover(s1)  # mints handles for t1's document
        s2 = scope_for_tab(sm, "t2", "main")
        inv = gw.invoke("searchProducts", {"query": "x"}, s2)
        self.assertFalse(inv.ok)
        # t2's page exposes no model context: fail closed, and the
        # transport's invoke was never touched.
        self.assertIn("WEBMCP_UNAVAILABLE", inv.result.error)
        self.assertEqual(tr.invoke_calls, [])

    def test_stale_handle_after_navigation(self):
        sm = make_sessions("t1", url="https://shop.example/a")
        tr = FakeModelContextTransport(
            tools=TOOLS, results={"searchProducts": {"ok": True,
                                                     "result": {"n": 1}}})
        gw = make_gateway(sm, tr)
        s1 = scope_for_tab(sm, "t1", "main")
        gw.discover(s1)
        # Navigate: the document identity is replaced.
        sm.navigate("t1", "https://shop.example/b")
        sm.register_frame("t1", "main", url="https://shop.example/b")
        inv = gw.invoke("searchProducts", {"query": "x"}, s1)
        self.assertFalse(inv.ok)
        self.assertIn("WEBMCP_STALE_HANDLE", inv.result.error)
        self.assertEqual(inv.error_code, "NAVIGATION_CHANGED")
        self.assertEqual(tr.invoke_calls, [])


class TestDeterministicSelection(unittest.TestCase):
    def test_selection_is_deterministic(self):
        sel = CapabilitySelector()
        r1 = sel.select("find running shoes in the catalog", TOOLS,
                        scope_key="k")
        r2 = sel.select("find running shoes in the catalog", TOOLS,
                        scope_key="k")
        self.assertEqual(r1.winner, r2.winner)
        self.assertEqual(r1.rationale, r2.rationale)
        self.assertEqual(r1.winner, "searchProducts")

    def test_selection_record_is_complete(self):
        sel = CapabilitySelector()
        rec = sel.select("add the laptop to my cart", TOOLS, scope_key="k")
        d = rec.to_dict()
        self.assertEqual(d["considered"], 2)
        self.assertEqual(len(d["candidates"]), 2)
        for c in d["candidates"]:
            self.assertIn("name", c)
            self.assertIn("score", c)
            self.assertIn("breakdown", c)
        self.assertEqual(d["winner"], "addToCart")
        self.assertTrue(d["rationale"])

    def test_no_winner_on_zero_scores(self):
        sel = CapabilitySelector()
        rec = sel.select("zzz qqq unrelated", TOOLS, scope_key="k")
        self.assertIsNone(rec.winner)

    def test_tie_break_prefers_lower_risk_then_name(self):
        sel = CapabilitySelector()
        tools = [
            {"name": "zetaSearch", "description": "search catalog",
             "input_schema": {}, "risk": "low"},
            {"name": "alphaSearch", "description": "search catalog",
             "input_schema": {}, "risk": "low"},
            {"name": "riskySearch", "description": "search catalog",
             "input_schema": {}, "risk": "high"},
        ]
        rec = sel.select("search catalog", tools, scope_key="k")
        # All three tie on score: low-risk pair beats high-risk; among
        # the pair, name ascending wins.
        scores = {c["name"]: c["score"]
                  for c in rec.to_dict()["candidates"]}
        self.assertEqual(scores["zetaSearch"], scores["alphaSearch"])
        self.assertEqual(scores["zetaSearch"], scores["riskySearch"])
        self.assertEqual(rec.winner, "alphaSearch")
        self.assertIn("alphaSearch", rec.rationale)


class TestGatewayIntegration(unittest.TestCase):
    """WebMCP invocation through ExecutionGateway: claims, evidence,
    verification, and the closed-loop contract."""

    def _stack(self, tools, results, allow_fallback=False, available=True):
        from harness.world_state import WorldState
        from harness.concurrency import OwnershipGraph, LeaseManager
        from harness.safety import SafetyLayer
        from harness.context_manager import ContextManager
        from harness.verification.verifier import Verifier
        from harness.tools import ToolExecutor
        from harness.transactions import TransactionEngine
        from harness.gateway import ExecutionGateway

        world = WorldState()
        ownership = OwnershipGraph()
        leases = LeaseManager(ownership)
        safety = SafetyLayer()
        ctx = ContextManager()
        verifier = Verifier(world)
        texec = ToolExecutor(safety)
        sessions = make_sessions()
        tr = FakeModelContextTransport(tools=tools, results=results,
                                       available=available)
        webmcp = WebMCPGateway(transport=tr, sessions=sessions,
                               allow_fallback=allow_fallback)
        texec.webmcp_gateway = webmcp
        texec.verifier = verifier
        engine = TransactionEngine(world, ownership, leases, texec,
                                   safety, ctx, verifier)
        gw = ExecutionGateway(engine, mem=None)
        return gw, verifier, tr, sessions

    def test_invoke_verifies_through_transaction_engine(self):
        gw, verifier, tr, sessions = self._stack(
            TOOLS, {"searchProducts": {"ok": True,
                                       "result": {"products": []}}})
        out = gw.execute({
            "actions": [{"tool": "webmcp_invoke",
                         "tool_name": "searchProducts",
                         "args": {"query": "shoes"}}],
            "task_id": "t-webmcp-1",
            "tab_id": "t1",
        })
        self.assertEqual(out["transaction_status"], "COMMITTED")
        ver = out["verifications"][0]
        self.assertEqual(ver["result"], "VERIFIED")
        self.assertEqual(tr.invoke_calls[0][1], "searchProducts")
        # The page's tool report is recorded as WEBMCP_RESULT evidence.
        ev_types = [e.evidence_type
                    for e in verifier.evidence_store.values()]
        self.assertIn("WEBMCP_RESULT", ev_types)

    def test_tool_reported_failure_verifies_as_failed(self):
        gw, verifier, tr, sessions = self._stack(
            TOOLS, {"searchProducts": {"ok": False,
                                       "error": "backend exploded"}})
        out = gw.execute({
            "actions": [{"tool": "webmcp_invoke",
                         "tool_name": "searchProducts",
                         "args": {"query": "shoes"}}],
            "task_id": "t-webmcp-2",
            "tab_id": "t1",
        })
        # The tool ran and reported failure: the action is FAILED (not a
        # silent success, not unverified). Failed dispatch carries no
        # verification entry -- there is nothing to verify.
        res = out["action_results"][0]
        self.assertEqual(res["state"], "FAILED")
        self.assertIn("backend exploded", res["error"])
        self.assertIsNone(out["verifications"][0])

    def test_partial_fallback_never_verifies(self):
        gw, verifier, tr, sessions = self._stack(
            [], {}, allow_fallback=True, available=False)
        out = gw.execute({
            "actions": [{"tool": "webmcp_invoke",
                         "tool_name": "searchProducts",
                         "args": {"query": "shoes"}}],
            "task_id": "t-webmcp-3",
            "tab_id": "t1",
        })
        res = out["action_results"][0]
        # The fixture answered (opt-in PARTIAL), but the claim must NOT
        # verify: fixture results prove nothing about the live page.
        self.assertTrue(res["partial_fallback"])
        ver = out["verifications"][0]
        self.assertNotEqual(ver["result"], "VERIFIED")
        self.assertIn("fixture", ver["reason"].lower())

    def test_partial_fallback_labels_its_caveat(self):
        gw, verifier, tr, sessions = self._stack(
            [], {}, allow_fallback=True, available=False)
        out = gw.execute({
            "actions": [{"tool": "webmcp_invoke",
                         "tool_name": "searchProducts",
                         "args": {"query": "shoes"}}],
            "task_id": "t-webmcp-4",
            "tab_id": "t1",
        })
        res = out["action_results"][0]
        self.assertTrue(res["partial_fallback"])
        self.assertIn("caveat", res)
        self.assertIn("PARTIAL", res["caveat"])


class TestCachedZeroToolDiscovery(unittest.TestCase):
    def test_repeat_cached_discovery_preserves_availability(self):
        sm = make_sessions()
        tr = FakeModelContextTransport(tools=[], available=True)
        gw = make_gateway(sm, tr)
        scope = scope_for_tab(sm, "t1", "main")
        first = gw.discover(scope)
        second = gw.discover(scope)  # served from the per-document cache
        self.assertTrue(first["available"])
        self.assertTrue(second["available"])
        self.assertEqual(tr.discover_calls, [scope.key()])

    def test_zero_tool_page_never_advertises(self):
        sm = make_sessions()
        gw = make_gateway(
            sm, FakeModelContextTransport(tools=[], available=True))
        scope = scope_for_tab(sm, "t1", "main")
        self.assertEqual(gw.advertised_tools(scope), [])
        # ... and a second discover round still sees nothing advertised
        # (the cache did not invent tools).
        gw.discover(scope)
        self.assertEqual(gw.advertised_tools(scope), [])


class TestCanonicalSession(unittest.TestCase):
    def test_scope_carries_canonical_session_id(self):
        sm = make_sessions()
        scope = scope_for_tab(sm, "t1", "main")
        self.assertIsNotNone(scope)
        self.assertIsNotNone(scope.session_id)
        # The stored id is the session manager's canonical id, not the
        # caller's None hint.
        self.assertEqual(
            scope.session_id,
            sm.tab_identity("t1")["session_id"])

    def test_scope_key_differs_per_document(self):
        sm = make_sessions("t1", url="https://shop.example/a")
        s1 = scope_for_tab(sm, "t1", "main")
        sm.navigate("t1", "https://shop.example/b")
        s2 = scope_for_tab(sm, "t1", "main")
        self.assertNotEqual(s1.key(), s2.key())


class TestUnknownFrame(unittest.TestCase):
    def test_unknown_frame_fails_closed(self):
        sm = make_sessions()
        self.assertIsNone(scope_for_tab(sm, "t1", "never-registered"))
        gw = make_gateway(sm, FakeModelContextTransport(tools=TOOLS))
        inv = gw.invoke("searchProducts", {"query": "x"},
                        scope_for_tab(sm, "t1", "never-registered"))
        self.assertFalse(inv.ok)
        self.assertIn("WEBMCP_SCOPE_VIOLATION", inv.result.error)

    def test_main_frame_always_known(self):
        sm = make_sessions()
        self.assertIsNotNone(scope_for_tab(sm, "t1", "main"))


class TestAuditTrail(unittest.TestCase):
    def test_discovery_selection_invocation_all_emitted(self):
        sm = make_sessions()
        events = []
        tr = FakeModelContextTransport(
            tools=TOOLS, results={"searchProducts": {"ok": True,
                                                     "result": {"n": 1}}})
        gw = WebMCPGateway(transport=tr, sessions=sm,
                           emit=lambda t, d: events.append(t))
        scope = scope_for_tab(sm, "t1", "main")
        gw.discover(scope)
        rec = gw.select("find shoes in the catalog", scope)
        self.assertIsNotNone(rec.winner)
        inv = gw.invoke("searchProducts", {"query": "shoes"}, scope)
        self.assertTrue(inv.ok)
        kinds = [e for e in events]
        self.assertIn("webmcp.discovered", kinds)
        self.assertIn("webmcp.selected", kinds)
        self.assertIn("webmcp.invoked", kinds)

    def test_rejection_and_fallback_emitted(self):
        sm = make_sessions()
        events = []
        tr = FakeModelContextTransport(tools=TOOLS)
        gw = WebMCPGateway(transport=tr, sessions=sm,
                           allow_fallback=True,
                           emit=lambda t, d: events.append((t, d)))
        scope = scope_for_tab(sm, "t1", "main")
        gw.discover(scope)
        bad = gw.invoke("deleteAccount", {}, scope)
        self.assertFalse(bad.ok)
        self.assertIn("webmcp.rejected", [t for t, _ in events])
        # Fallback path emits its own event with the PARTIAL caveat.
        gw2 = WebMCPGateway(
            transport=FakeModelContextTransport(tools=[], available=False),
            sessions=sm, allow_fallback=True,
            emit=lambda t, d: events.append((t, d)))
        fb = gw2.invoke("searchProducts", {"query": "x"}, scope)
        self.assertTrue(fb.partial)
        partial_events = [d for t, d in events if t == "webmcp.partial_fallback"]
        self.assertEqual(len(partial_events), 1)
        self.assertIn("PARTIAL", partial_events[0]["caveat"])


class TestClosedLoopWebMCP(unittest.TestCase):
    def _orch(self, tools, results, available=True):
        from harness.orchestrator import (AgentLoop, Orchestrator,
                                          TaskExecutionContext)
        from harness.verification.verifier import Verifier
        from harness.world_state import WorldState
        from harness.concurrency import LeaseManager, OwnershipGraph

        sm = make_sessions()
        tr = FakeModelContextTransport(tools=tools, results=results,
                                       available=available)
        gw = WebMCPGateway(transport=tr, sessions=sm)
        state = type("S", (), {})()
        state.webmcp_gateway = gw
        state.verifier = Verifier(WorldState())
        state.leases = LeaseManager(OwnershipGraph())
        state.sessions = sm
        loop = AgentLoop(state, Orchestrator())
        ctx = TaskExecutionContext(task_id="t-cl-1",
                                   goal="find running shoes in the catalog",
                                   tab_id="t1")
        return loop, state, ctx, tr

    def test_selection_record_preserved_on_action(self):
        orch, state, ctx, tr = self._orch(TOOLS, {})
        sel = orch._select_webmcp_action(ctx)
        self.assertIsNotNone(sel)
        self.assertEqual(sel["tool"], "webmcp_invoke")
        self.assertEqual(sel["tool_name"], "searchProducts")
        rec = sel["webmcp_selection"]
        self.assertEqual(rec["winner"], "searchProducts")
        self.assertEqual(rec["considered"], 2)
        self.assertTrue(rec["rationale"])
        # ... and the note went to the task's audit notes.
        self.assertTrue(any(n[0] == "webmcp_selection"
                            for n in ctx.history))

    def test_no_selection_without_advertised_tools(self):
        orch, state, ctx, tr = self._orch([], {}, available=True)
        self.assertIsNone(orch._select_webmcp_action(ctx))

    def test_failed_report_recorded_honestly(self):
        from harness.verification.evidence import WEBMCP_RESULT
        orch, state, ctx, tr = self._orch(TOOLS, {})
        out = orch.report(
            ctx, action_id="a1", claim_id=None, status="browser_failed",
            reason="tool blew up",
            webmcp_result={"tool": "searchProducts", "ok": False,
                           "error": "backend exploded", "tab_id": "t1"})
        # The failure is replanned, but the page's own failure report is
        # recorded as evidence first -- it is not silently dropped.
        self.assertEqual(out["decision"], "replan")
        ev = [e for e in state.verifier.evidence_store.values()
              if e.evidence_type == WEBMCP_RESULT]
        self.assertEqual(len(ev), 1)
        self.assertFalse(ev[0].payload["ok"])
        self.assertEqual(ev[0].payload["error"], "backend exploded")

    def test_mismatched_report_rejected(self):
        orch, state, ctx, tr = self._orch(TOOLS, {})
        out = orch.report(
            ctx, action_id="a1", claim_id=None, status="executed",
            webmcp_result={"tool": "searchProducts", "ok": "yes",
                           "tab_id": "t1"})
        self.assertEqual(out["decision"], "replan")
        self.assertIn("webmcp report rejected", out["reason"])


if __name__ == "__main__":
    unittest.main()
