"""Live verification: CDP WebMCP transport against a page-advertised model context.

The fixture page (fixtures/webmcp.html) stubs navigator.modelContext in
the WebMCP proposal shape the gateway probes for (async availableTools()
+ invokeTool()). The test drives the REAL CdpModelContextTransport --
probe and invoke JS evaluated inside the live page via
OwnedBrowserRuntime.evaluate_js -- through the REAL WebMCPGateway:
discovery, deterministic selection, scoped invocation, schema
validation, and the fail-closed paths (no model context, unadvertised
tool). A webmcp_invoke claim is then verified against the page's own
tool report as WEBMCP_RESULT evidence.

The stub page is scaffolding, not a production site; what is verified
live is the transport + gateway behavior against a real page exposing
the proposal shape.
"""
import shutil
import sys
import time
import unittest
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from tests.live.helpers import (
    FixtureServer, container_chrome_args, live_profile_dir,
    skip_unless_browser, wait_until,
)


@skip_unless_browser("live WebMCP test needs a real browser")
class TestLiveWebMCP(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from harness.browser_owned import OwnedBrowserRuntime
        from harness.webmcp.gateway import WebMCPGateway
        from harness.webmcp.scope import WebMCPScope
        from harness.webmcp.transport import CdpModelContextTransport
        cls.server = FixtureServer()
        cls.base_url = cls.server.start()
        cls.profile_dir = live_profile_dir(prefix="synk-live-webmcp-")
        cls.runtime = OwnedBrowserRuntime.launch(
            profile_dir=cls.profile_dir, headless=True,
            extra_args=container_chrome_args())
        cls.runtime.connect()
        cls.tab = cls.runtime.new_tab(cls.base_url + "/webmcp.html")
        cls.plain_tab = cls.runtime.new_tab(cls.base_url + "/plain.html")
        wait_until(
            lambda: cls.runtime.evaluate_js(
                cls.tab, "main",
                "() => !!(navigator.modelContext && "
                "navigator.modelContext.availableTools)") is True,
            desc="modelContext stub present in live page")
        transport = CdpModelContextTransport(
            evaluate=cls.runtime.evaluate_js)
        cls.gateway = WebMCPGateway(transport, sessions=None)
        cls.scope = WebMCPScope(session_id="live-session", tab_id=cls.tab,
                                frame_id="main", document_id=None)
        cls.plain_scope = WebMCPScope(
            session_id="live-session", tab_id=cls.plain_tab,
            frame_id="main", document_id=None)

    @classmethod
    def tearDownClass(cls):
        try:
            cls.runtime.disconnect()
        finally:
            cls.server.stop()
            shutil.rmtree(cls.profile_dir, ignore_errors=True)

    def test_discovery_lists_advertised_tools(self):
        disc = self.gateway.discover(self.scope)
        self.assertTrue(disc["ok"])
        self.assertTrue(disc["available"],
                        f"model context not detected: {disc}")
        names = {t["name"] for t in disc["tools"]}
        self.assertEqual(names, {"getOrderStatus", "cancelOrder"})
        # Handles are minted per (scope, tool), deterministically.
        self.assertEqual(set(disc["handles"]), names)
        get_status = next(t for t in disc["tools"]
                          if t["name"] == "getOrderStatus")
        self.assertTrue(get_status["annotations"]["readOnlyHint"])
        cancel = next(t for t in disc["tools"]
                      if t["name"] == "cancelOrder")
        self.assertTrue(cancel["annotations"]["destructiveHint"])

    def test_selection_is_deterministic_and_recorded(self):
        rec = self.gateway.select("check the status of order o-1",
                                  self.scope)
        self.assertEqual(rec.winner, "getOrderStatus")
        self.assertTrue(rec.candidates)
        # Deterministic: same goal, same winner.
        rec2 = self.gateway.select("check the status of order o-1",
                                   self.scope)
        self.assertEqual(rec2.winner, rec.winner)

    def test_invoke_round_trip_through_page(self):
        inv = self.gateway.invoke(
            "getOrderStatus", {"order_id": "o-123"}, self.scope,
            task_id="t_live_wm", action_id="a_live_wm1")
        self.assertTrue(inv.ok, f"invoke failed: {inv.result.error}")
        self.assertFalse(inv.partial)
        self.assertEqual(inv.result.result["status"], "shipped")
        self.assertEqual(inv.result.result["order_id"], "o-123")
        self.assertIsNotNone(inv.handle)
        self.assertEqual(inv.handle.tool_name, "getOrderStatus")

    def test_invoke_claim_verified_by_page_report(self):
        # The page's own tool report, recorded as WEBMCP_RESULT evidence,
        # verifies the webmcp_result postcondition. Fixture-fallback
        # evidence is excluded by construction (partial=False here).
        from harness.verification.evidence import Evidence, WEBMCP_RESULT
        from harness.verification.claims import Claim
        from harness.verification.verifier import Verifier
        from harness.world_state import WorldState
        from harness.verification.evidence import strength_of
        verifier = Verifier(WorldState())
        inv = self.gateway.invoke(
            "getOrderStatus", {"order_id": "o-9"}, self.scope,
            task_id="t_live_wmc", action_id="a_live_wmc1")
        self.assertTrue(inv.ok)
        res = inv.result
        verifier.record_evidence(Evidence(
            evidence_id=f"e_live_{uuid.uuid4().hex[:8]}",
            evidence_type=WEBMCP_RESULT, source="webmcp",
            timestamp=time.time(), action_id="a_live_wmc1",
            task_id="t_live_wmcc",
            payload={"tool": "getOrderStatus", "ok": res.ok,
                     "result": res.result, "error": res.error,
                     "partial_fallback": inv.partial},
            strength=strength_of(WEBMCP_RESULT),
            provenance="page_model_context"))
        claim_id = f"c_live_{uuid.uuid4().hex[:8]}"
        verifier.propose_claim(Claim(
            claim_id=claim_id, task_id="t_live_wmc", actor="agent",
            claim_type="ACTION_COMPLETED", target="getOrderStatus",
            requested_state={}, claimed_state=None,
            action_ids=["a_live_wmc1"], tool="webmcp_invoke",
            postcondition={"kind": "webmcp_result",
                           "tool_name": "getOrderStatus"}))
        result = verifier.verify(claim_id)
        self.assertEqual(result.result, "VERIFIED")
        self.assertIn("reported success", result.reason)

    def test_fail_closed_without_model_context(self):
        disc = self.gateway.discover(self.plain_scope)
        self.assertTrue(disc["ok"])
        self.assertFalse(disc["available"])
        self.assertIn("modelContext", disc["reason"])
        inv = self.gateway.invoke(
            "getOrderStatus", {"order_id": "o-1"}, self.plain_scope,
            task_id="t_live_wmf", action_id="a_live_wmf1")
        self.assertFalse(inv.ok)
        self.assertIn("WEBMCP_UNAVAILABLE", inv.result.error)
        self.assertIn("fixture", inv.result.error)

    def test_fail_closed_unadvertised_tool(self):
        inv = self.gateway.invoke(
            "deleteEverything", {}, self.scope,
            task_id="t_live_wmu", action_id="a_live_wmu1")
        self.assertFalse(inv.ok)
        self.assertIn("WEBMCP_TOOL_NOT_ADVERTISED", inv.result.error)

    def test_schema_validation_rejects_bad_args(self):
        inv = self.gateway.invoke(
            "getOrderStatus", {}, self.scope,  # order_id required
            task_id="t_live_wms", action_id="a_live_wms1")
        self.assertFalse(inv.ok)
        self.assertIn("WEBMCP_SCHEMA_INVALID", inv.result.error)
