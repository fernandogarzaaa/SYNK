"""L3 vision: ladder selection + honest screenshot-evidence semantics."""
import sys
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from harness.ladder import ExecutionLadder
from harness.verification.verifier import Verifier
from harness.verification.claims import Claim, CLAIM_TYPE_STATE
from harness.verification.evidence import Evidence, SCREENSHOT


class TestLadderVision(unittest.TestCase):
    def test_l3_selected_when_vision_available(self):
        level, _, _ = ExecutionLadder().choose_level(
            "x.com", "read chart on dashboard", vision_available=True)
        self.assertEqual(level, 3)

    def test_human_handoff_without_vision(self):
        level, _, _ = ExecutionLadder().choose_level(
            "x.com", "read chart on dashboard", vision_available=False)
        self.assertEqual(level, 5)


class TestScreenshotEvidence(unittest.TestCase):
    def test_screenshot_alone_never_verifies(self):
        v = Verifier(None)
        v.propose_claim(Claim("c", "t", "agent", CLAIM_TYPE_STATE,
                              "chart", None, "read", action_ids=["a"]))
        v.record_evidence(Evidence("e", SCREENSHOT, "runtime", time.time(),
                                   action_id="a", task_id="t",
                                   payload={"sha256": "abc", "bytes": 123}))
        res = v.verify("c")
        self.assertNotEqual(res.result, "FAILED")
        self.assertNotEqual(res.result, "VERIFIED")
        self.assertEqual(res.result, "UNVERIFIED")


if __name__ == "__main__":
    unittest.main()
