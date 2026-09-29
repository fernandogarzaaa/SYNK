"""Stage B regression tests: identity, event-sourced WorldState, ElementRef,
exclusive leases. Run: python -m unittest discover -s tests -v"""
import sys
import threading
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from harness.session import SessionManager, new_id, stable_id, canonical_origin
from harness.world_state import WorldState, snapshot_hash
from harness.context_manager import ContextManager, fingerprint_of
from harness.concurrency import (OwnershipGraph, LeaseManager, FREE,
                                 HUMAN_OWNED, AGENT_OWNED, CONFLICT,
                                 hierarchy_for)


def shop_nodes():
    return [
        {"role": "textbox", "name": "Email", "tag": "input",
         "selector": "#email", "interactive": True, "index": 0,
         "value": "", "disabled": False},
        {"role": "button", "name": "Submit", "tag": "button",
         "selector": "#submit", "interactive": True, "index": 1,
         "disabled": False},
    ]


class TestSessionIdentity(unittest.TestCase):
    def test_tab_identity_resolution(self):
        sm = SessionManager()
        sm.register_tab("t1", "w1", url="https://a.com/x", title="A")
        ident = sm.tab_identity("t1")
        self.assertIsNotNone(ident)
        self.assertEqual(ident["tab_id"], "t1")
        self.assertEqual(ident["window_id"], "w1")
        self.assertIn("session_id", ident)
        self.assertIsNone(sm.tab_identity("nope"))

    def test_observation_versions_monotonic(self):
        sm = SessionManager()
        o1 = sm.record_observation("t1", snapshot_hash="aaa")
        o2 = sm.record_observation("t1", snapshot_hash="bbb")
        self.assertEqual(o2["observation_version"], o1["observation_version"] + 1)
        self.assertNotEqual(o1["observation_id"], o2["observation_id"])

    def test_stable_ids_deterministic(self):
        self.assertEqual(stable_id("x", "a", "b"), stable_id("x", "a", "b"))
        self.assertNotEqual(stable_id("x", "a", "b"), stable_id("x", "a", "c"))

    def test_canonical_origin(self):
        self.assertEqual(canonical_origin("https://example.com:8443/p"),
                         "https://example.com:8443")
        self.assertNotEqual(canonical_origin("http://example.com/"),
                            canonical_origin("https://example.com/"))

    def test_new_ids_unique(self):
        self.assertNotEqual(new_id("t"), new_id("t"))


class TestEventSourcedWorld(unittest.TestCase):
    def test_value_changed_updates_canonical_state(self):
        w = WorldState()
        w.load_full("https://shop.com", shop_nodes(), tab_id="t1")
        w.apply_event({"type": "value.changed",
                       "data": {"tab_id": "t1", "target": "#email",
                                "value": "inan@x.com"}})
        self.assertEqual(w.element_value("t1", "#email"), "inan@x.com")
        st = w.element_state("t1", "#email")
        self.assertEqual(st["value"], "inan@x.com")

    def test_dom_changed_updates_disabled(self):
        w = WorldState()
        w.load_full("https://shop.com", shop_nodes(), tab_id="t1")
        self.assertFalse(w.element_state("t1", "#submit")["disabled"])
        w.apply_event({"type": "dom.changed",
                       "data": {"tab_id": "t1", "target": "#submit",
                                "state": {"disabled": True}}})
        self.assertTrue(w.element_state("t1", "#submit")["disabled"])

    def test_journal_append_only_and_monotonic(self):
        w = WorldState()
        v0 = w.version
        w.load_full("https://a.com", shop_nodes(), tab_id="t1")
        w.apply_event({"type": "focus.changed",
                       "data": {"tab_id": "t1", "target": "#email"}})
        self.assertGreater(w.version, v0)
        seqs = [e["seq"] for e in w.journal.events()]
        self.assertEqual(seqs, sorted(seqs))
        self.assertEqual(len(w.journal), 2)

    def test_replay_is_deterministic(self):
        w1 = WorldState()
        w1.load_full("https://shop.com", shop_nodes(), tab_id="t1")
        w1.apply_event({"type": "value.changed",
                        "data": {"tab_id": "t1", "target": "#email",
                                 "value": "a@b.c"}})
        w2 = WorldState().replay(w1.journal.events())
        self.assertEqual(w1.tabs["t1"]["snapshot_hash"],
                         w2.tabs["t1"]["snapshot_hash"])
        self.assertEqual(w2.element_value("t1", "#email"), "a@b.c")

    def test_navigation_invalidates_element_state(self):
        w = WorldState()
        w.load_full("https://shop.com", shop_nodes(), tab_id="t1")
        w.apply_event({"type": "value.changed",
                       "data": {"tab_id": "t1", "target": "#email",
                                "value": "a@b.c"}})
        w.apply_event({"type": "page.navigated",
                       "data": {"tab_id": "t1", "url": "https://shop.com/done"}})
        self.assertIsNone(w.element_value("t1", "#email"))
        self.assertEqual(w.tabs["t1"]["url"], "https://shop.com/done")

    def test_observation_version_bumps_on_mutation(self):
        w = WorldState()
        w.load_full("https://shop.com", shop_nodes(), tab_id="t1")
        v1 = w.tab_observation("t1")["observation_version"]
        w.apply_event({"type": "value.changed",
                       "data": {"tab_id": "t1", "target": "#email",
                                "value": "x"}})
        v2 = w.tab_observation("t1")["observation_version"]
        self.assertGreater(v2, v1)

    def test_dialog_state_canonical(self):
        w = WorldState()
        w.load_full("https://shop.com", shop_nodes(), tab_id="t1")
        w.apply_event({"type": "dialog.opened",
                       "data": {"tab_id": "t1", "target": "#modal"}})
        self.assertEqual(w.tabs["t1"]["modal"], "#modal")
        w.apply_event({"type": "dialog.closed", "data": {"tab_id": "t1"}})
        self.assertIsNone(w.tabs["t1"]["modal"])


class TestElementRef(unittest.TestCase):
    def test_current_ref_resolves(self):
        cm = ContextManager()
        view = cm.ingest("https://a.com", shop_nodes(), tab_id="t1")
        ref = next(iter(cm.refs))
        meta = cm.resolve_ref(ref, view["snapshot_version"], tab_id="t1")
        self.assertIsNotNone(meta)
        self.assertEqual(meta["tab_id"], "t1")
        self.assertEqual(meta["origin"], "https://a.com")

    def test_stale_ref_rejected_even_with_latest_version(self):
        # The old inverted check let an old ref resolve when the caller
        # passed the latest version. That must now fail closed.
        cm = ContextManager()
        v1 = cm.ingest("https://a.com", shop_nodes(), tab_id="t1")
        old_ref = next(iter(cm.refs))
        v2 = cm.ingest("https://a.com", shop_nodes(), tab_id="t1")
        # old version -> reject
        self.assertIsNone(cm.resolve_ref(old_ref, v1["snapshot_version"],
                                         tab_id="t1"))
        # latest version passed for an OLD ref -> still reject
        self.assertIsNone(cm.resolve_ref(old_ref, v2["snapshot_version"],
                                         tab_id="t1"))
        # current ref resolves
        new_ref = max(r for r in cm.refs if r != old_ref)
        self.assertIsNotNone(cm.resolve_ref(new_ref, v2["snapshot_version"],
                                            tab_id="t1"))

    def test_ref_rejected_on_tab_origin_frame_mismatch(self):
        cm = ContextManager()
        view = cm.ingest("https://a.com", shop_nodes(), tab_id="t1")
        ref = next(iter(cm.refs))
        v = view["snapshot_version"]
        self.assertIsNone(cm.resolve_ref(ref, v, tab_id="other-tab"))
        self.assertIsNone(cm.resolve_ref(ref, v, origin="https://evil.com"))
        self.assertIsNone(cm.resolve_ref(ref, v, frame_id="sub:1234"))
        # tab_id omitted is allowed when the version is the tab's CURRENT one
        self.assertIsNotNone(cm.resolve_ref(ref, v))

    def test_ref_rejected_on_fingerprint_mismatch(self):
        cm = ContextManager()
        view = cm.ingest("https://a.com", shop_nodes(), tab_id="t1")
        ref = next(iter(cm.refs))
        v = view["snapshot_version"]
        fp = dict(cm.refs[ref]["fingerprint"])
        fp["disabled"] = not fp["disabled"]
        self.assertIsNone(cm.resolve_ref(ref, v, fingerprint=fp))
        # matching fingerprint resolves
        self.assertIsNotNone(
            cm.resolve_ref(ref, v, fingerprint=cm.refs[ref]["fingerprint"]))

    def test_unknown_ref_rejected(self):
        cm = ContextManager()
        cm.ingest("https://a.com", shop_nodes())
        self.assertIsNone(cm.resolve_ref(99999, 1))

    def test_diff_detects_value_and_disabled_changes(self):
        old = shop_nodes()
        new = shop_nodes()
        new[0] = dict(new[0], value="typed!")
        new[1] = dict(new[1], disabled=True)
        summary = ContextManager.diff_summary(old, new)
        self.assertIn("value changed", summary)
        self.assertIn("disabled changed", summary)
        self.assertIn("#email", summary)

    def test_diff_detects_added_removed(self):
        old = shop_nodes()
        new = [shop_nodes()[0]]
        summary = ContextManager.diff_summary(old, new)
        self.assertIn("-1 elements", summary)

    def test_fingerprint_value_hash(self):
        n1 = {"role": "textbox", "name": "e", "tag": "input",
              "selector": "#email", "value": "a"}
        n2 = dict(n1, value="b")
        self.assertNotEqual(fingerprint_of(n1)["value_hash"],
                            fingerprint_of(n2)["value_hash"])
        self.assertEqual(fingerprint_of(n1)["value_hash"],
                         fingerprint_of(dict(n1))["value_hash"])


class TestExclusiveLeases(unittest.TestCase):
    def test_two_agents_race_one_wins(self):
        og = OwnershipGraph()
        lm = LeaseManager(og)
        winners = []
        barrier = threading.Barrier(8)

        def racer():
            barrier.wait()
            lease = lm.acquire("#pay", intent="click")
            if lease:
                winners.append(lease["lease"])

        threads = [threading.Thread(target=racer) for _ in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(len(winners), 1)
        self.assertEqual(og.state_of("#pay"), AGENT_OWNED)

    def test_second_acquire_rejected_while_held(self):
        og = OwnershipGraph()
        lm = LeaseManager(og)
        first = lm.acquire("#pay")
        self.assertIsNotNone(first)
        self.assertIsNone(lm.acquire("#pay"))
        self.assertTrue(lm.release("#pay", first["lease"]))
        # after release, acquisition works again
        self.assertIsNotNone(lm.acquire("#pay"))

    def test_stale_lease_cannot_release_newer(self):
        og = OwnershipGraph()
        lm = LeaseManager(og)
        a = lm.acquire("#pay")
        self.assertTrue(lm.release("#pay", a["lease"]))
        b = lm.acquire("#pay")
        self.assertIsNotNone(b)
        # stale lease A tries to release B's ownership -> refused
        self.assertFalse(lm.release("#pay", a["lease"]))
        self.assertEqual(og.state_of("#pay"), AGENT_OWNED)
        self.assertEqual(og.owner_of("#pay")["lease"], b["lease"])
        # correct release works
        self.assertTrue(lm.release("#pay", b["lease"]))
        self.assertEqual(og.state_of("#pay"), FREE)

    def test_lease_expiry_and_prune(self):
        og = OwnershipGraph()
        lm = LeaseManager(og)
        lease = lm.acquire("#pay", ttl=0.1)
        self.assertIsNotNone(lease)
        time.sleep(0.25)
        pruned = lm.prune()
        self.assertGreaterEqual(pruned, 1)
        self.assertEqual(og.state_of("#pay"), FREE)
        self.assertIsNotNone(lm.acquire("#pay"))

    def test_agent_vs_human_ownership(self):
        og = OwnershipGraph()
        lm = LeaseManager(og)
        og.mark_human("#email")
        self.assertEqual(og.state_of("#email"), HUMAN_OWNED)
        self.assertIsNone(lm.acquire("#email"))
        # human ownership decays: after TTL the agent may acquire
        og.mark_human("#other", ttl=0.05)
        time.sleep(0.1)
        og.prune()
        self.assertIsNotNone(lm.acquire("#other"))

    def test_human_touch_on_agent_target_is_conflict(self):
        og = OwnershipGraph()
        lm = LeaseManager(og)
        lease = lm.acquire("#pay")
        self.assertIsNotNone(lease)
        state = og.mark_human("#pay")
        self.assertEqual(state, CONFLICT)
        self.assertEqual(og.state_of("#pay"), CONFLICT)

    def test_hierarchy_blocks_leaf_acquire(self):
        og = OwnershipGraph()
        lm = LeaseManager(og)
        og.mark_human("tab:7")
        lease = lm.acquire("tab:7#email",
                           owner_hierarchy=hierarchy_for("7"))
        self.assertIsNone(lease)
        # without the blocked ancestor, acquisition works
        lease2 = lm.acquire("tab:8#email",
                            owner_hierarchy=hierarchy_for("8"))
        self.assertIsNotNone(lease2)

    def test_emergency_stop(self):
        og = OwnershipGraph()
        lm = LeaseManager(og)
        lm.acquire("#a")
        lm.acquire("#b")
        revoked = lm.emergency_stop()
        self.assertEqual(revoked, 2)
        self.assertIsNone(lm.acquire("#c"))
        self.assertEqual(og.state_of("#a"), FREE)
        lm.clear_emergency()
        self.assertIsNotNone(lm.acquire("#c"))

    def test_ttl_bounded(self):
        og = OwnershipGraph()
        lm = LeaseManager(og)
        lease = lm.acquire("#pay", ttl=3600)
        self.assertIsNotNone(lease)
        self.assertLessEqual(lease["ttl"], 30.0)

    def test_graph_acquire_agent_rejects_foreign_lease(self):
        og = OwnershipGraph()
        self.assertTrue(og.acquire_agent("#x", "lease_aaa"))
        self.assertFalse(og.acquire_agent("#x", "lease_bbb"))
        # same lease may renew
        self.assertTrue(og.acquire_agent("#x", "lease_aaa"))
        # compare-and-release with wrong id fails
        self.assertFalse(og.release("#x", "lease_bbb"))
        self.assertTrue(og.release("#x", "lease_aaa"))

    def test_unknown_lease_release_fails(self):
        og = OwnershipGraph()
        lm = LeaseManager(og)
        self.assertFalse(lm.release("#nope", "lease_ghost"))


class TestServerWiring(unittest.TestCase):
    def test_state_has_session_manager(self):
        from harness.server import State
        st = State(":memory:")
        self.assertIsNotNone(st.sessions)
        self.assertEqual(st.world.version, 0)

    def test_transact_carries_tab_identity(self):
        # Identity flows into actions without a live browser: use a mock tool.
        from harness.concurrency import TransactionRunner
        from harness.world_state import WorldState

        class Tools:
            def run(self, action, page_url="", user_consented=False,
                    tab_id="default"):
                self.last_tab = tab_id
                return {"ok": True, "command": action.get("tool")}
        og = OwnershipGraph()
        lm = LeaseManager(og)
        tools = Tools()
        from harness.safety import SafetyLayer
        tx = TransactionRunner(WorldState(), og, lm, tools, SafetyLayer())
        # Stage F: default-deny policy requires the test origin registered.
        tx.engine.policy.register_origin(
            "x.com", allow=["read", "navigate", "interact"],
            description="stage B test origin")
        res = tx.run({"tool": "click", "target": "#b", "tab_id": "t9"},
                     "https://x.com", tab_id="t9")
        self.assertEqual(res["verdict"], "executed")
        self.assertEqual(tools.last_tab, "t9")


if __name__ == "__main__":
    unittest.main()
