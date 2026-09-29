"""Live verification: the shipped sequential TaskScheduler driving a REAL
browser with independently verified actions.

harness.task_scheduler.TaskScheduler is the Stage G runner:
EXECUTION_MODE = "SEQUENTIAL", lease-aware dispatch, and an exec_fn
contract (task dict -> dict with at least {"ok": bool}). The exec_fn
below is the only honest wiring: it executes through the REAL
TransactionEngine, then re-observes the live page with a FRESH
snapshot, records independent evidence, and only returns ok=True when
every claim is VERIFIED. Scheduler completion therefore depends on
verified action results, not on the executor's say-so.

Negative control: an exec_fn that dispatches but never verifies returns
ok=False, and the task must end "failed" -- never "completed".
"""
import shutil
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from tests.live.helpers import (
    FixtureServer, container_chrome_args, find_node, live_profile_dir,
    make_live_engine, record_independent_observation, skip_unless_browser,
    wait_until,
)
from harness.verification.evidence import ELEMENT_STATE


def _make_exec_fn(runtime, engine, verify_actions):
    """The honest exec_fn: execute live, verify independently.

    verify_actions=True: re-observe each action from a fresh snapshot,
    record independent evidence, and report ok=True only when every
    claim verifies. verify_actions=False: dispatch without any
    independent observation (verification stays UNVERIFIED -> ok=False).
    """
    def exec_fn(task):
        report = engine.execute(
            task["actions"], task_id=task["task_id"],
            tab_id=task["tab_id"], page_url=task["page_url"])
        all_ok = True
        for ex, action in zip(report.executions, task["actions"]):
            if verify_actions and "selector" in action:
                node = find_node(
                    runtime.snapshot(task["tab_id"])["nodes"],
                    action["selector"])
                record_independent_observation(
                    engine.verifier, evidence_type=ELEMENT_STATE,
                    action_id=ex.request.action_id,
                    task_id=task["task_id"],
                    payload={"target": action["selector"],
                             "value": node.get("value")
                             if node else None})
            result = engine.verifier.verify(ex.claim_id)
            if result.result != "VERIFIED":
                all_ok = False
        return {"ok": all_ok,
                "executions": len(report.executions)}
    return exec_fn


@skip_unless_browser("live scheduler test needs a real browser")
class TestLiveTaskScheduler(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from harness.browser_owned import OwnedBrowserRuntime
        cls.server = FixtureServer()
        cls.base_url = cls.server.start()
        cls.checkout_url = cls.base_url + "/checkout.html"
        cls.profile_dir = live_profile_dir(prefix="synk-live-sched-")
        cls.runtime = OwnedBrowserRuntime.launch(
            profile_dir=cls.profile_dir, headless=True,
            extra_args=container_chrome_args())
        cls.runtime.connect()
        cls.tab = cls.runtime.new_tab(cls.checkout_url)
        cls.engine = make_live_engine(cls.runtime)

    @classmethod
    def tearDownClass(cls):
        try:
            cls.runtime.disconnect()
        finally:
            cls.server.stop()
            shutil.rmtree(cls.profile_dir, ignore_errors=True)

    def setUp(self):
        self.runtime.navigate(self.checkout_url, tab_id=self.tab)
        wait_until(
            lambda: find_node(
                self.runtime.snapshot(self.tab)["nodes"], "#email"),
            desc="checkout form present")

    def _spec(self, task_id, text):
        return {"task_id": task_id,
                "goal": "type the contact email on the checkout page",
                "actions": [{"tool": "type", "selector": "#email",
                             "text": text}],
                "tab_id": self.tab, "page_url": self.checkout_url}

    def test_run_next_completes_on_verified_actions(self):
        from harness.task_scheduler import TaskScheduler
        sched = TaskScheduler(leases=self.engine.live_leases)
        task = sched.submit(self._spec("t-live-sched-1",
                                       "scheduled@example.com"))
        self.assertEqual(task.status, "queued")
        done = sched.run_next(_make_exec_fn(
            self.runtime, self.engine, verify_actions=True))
        self.assertEqual(done["status"], "completed")
        self.assertTrue(done["result"]["ok"])
        self.assertEqual(done["execution_mode"], "SEQUENTIAL")
        # The page really holds the typed value (independent check, not
        # the scheduler's word).
        node = find_node(
            self.runtime.snapshot(self.tab)["nodes"], "#email")
        self.assertEqual(node.get("value"), "scheduled@example.com")
        # The tab lease was acquired for dispatch and released after.
        self.assertEqual(
            sched.status("t-live-sched-1")["status"], "completed")

    def test_sequential_second_task_waits(self):
        from harness.task_scheduler import TaskScheduler
        sched = TaskScheduler(leases=self.engine.live_leases)
        sched.submit(self._spec("t-live-sched-a", "first@example.com"))
        sched.submit(self._spec("t-live-sched-b", "second@example.com"))
        exec_fn = _make_exec_fn(self.runtime, self.engine,
                                verify_actions=True)
        first = sched.run_next(exec_fn)
        self.assertEqual(first["task_id"], "t-live-sched-a")
        self.assertEqual(first["status"], "completed")
        # Sequential: one dispatch per run_next; the second task waited.
        self.assertEqual(sched.status("t-live-sched-b")["status"],
                         "queued")
        second = sched.run_next(exec_fn)
        self.assertEqual(second["task_id"], "t-live-sched-b")
        self.assertEqual(second["status"], "completed")
        node = find_node(
            self.runtime.snapshot(self.tab)["nodes"], "#email")
        self.assertEqual(node.get("value"), "second@example.com")

    def test_task_fails_when_actions_unverified(self):
        # The exec_fn dispatches the action but never verifies; the
        # scheduler must NOT mark the task completed.
        from harness.task_scheduler import TaskScheduler
        sched = TaskScheduler(leases=self.engine.live_leases)
        sched.submit(self._spec("t-live-sched-neg",
                                "never-verified@example.com"))
        done = sched.run_next(_make_exec_fn(
            self.runtime, self.engine, verify_actions=False))
        self.assertEqual(done["status"], "failed")
        self.assertFalse(done["result"]["ok"])
