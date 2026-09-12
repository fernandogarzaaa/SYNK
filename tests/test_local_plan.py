"""Regression: local-runtime decisions must never emit tools outside the allowlist."""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from harness.server import _normalize_local_decision
from harness.tools import TOOL_SCHEMAS


class TestLocalPlan(unittest.TestCase):
    def test_fill_aliases_to_type(self):
        a = _normalize_local_decision({"decision": "fill", "ref": 1, "text": "x"}, "g")
        self.assertIsNotNone(a)
        self.assertIn(a["tool"], TOOL_SCHEMAS)

    def test_unknown_tool_escalates(self):
        self.assertIsNone(_normalize_local_decision({"decision": "teleport"}, "g"))
        self.assertIsNone(_normalize_local_decision({"decision": "escalate"}, "g"))
        self.assertIsNone(_normalize_local_decision({"decision": "bulk"}, "g"))

    def test_valid_tools_pass_through(self):
        for tool in ("click", "type", "navigate", "scroll"):
            a = _normalize_local_decision({"decision": tool, "ref": 2}, "g")
            self.assertIsNotNone(a)
            self.assertEqual(a["tool"], tool)


if __name__ == "__main__":
    unittest.main()
