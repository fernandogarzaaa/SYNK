"""Local model backends: endpoint adapter (mocked transport) + fail-safe routing."""
import io
import json
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from harness.local.model import (
    EndpointLocalModel, MockLocalModel, build_local_model, InferenceRequest,
)
from harness.local.runtime import LocalRuntime


def _fake_response(payload: dict):
    class Ctx:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self):
            return json.dumps(payload).encode()
    return Ctx()


class TestEndpointModel(unittest.TestCase):
    def test_parses_decision_and_confidence(self):
        m = EndpointLocalModel(url="http://x/v1/chat/completions", model="t")
        body = {"choices": [{"message": {"content":
                 '{"decision": "click", "ref": 3, "confidence": 0.97}'}}]}
        with patch("urllib.request.urlopen", return_value=_fake_response(body)):
            res = m.infer(InferenceRequest(prompt="p", context={}))
        self.assertEqual(res.confidence, 0.97)
        self.assertIn("click", res.text)
        self.assertTrue(res.provider.startswith("local-endpoint:"))

    def test_transport_error_raises(self):
        m = EndpointLocalModel(url="http://x/v1/chat/completions", model="t")
        with patch("urllib.request.urlopen", side_effect=ConnectionError("down")):
            with self.assertRaises(ConnectionError):
                m.infer(InferenceRequest(prompt="p", context={}))

    def test_factory_defaults_to_mock(self):
        import os
        os.environ.pop("SYNK_LOCAL_MODEL", None)
        self.assertIsInstance(build_local_model(), MockLocalModel)

    def test_factory_endpoint(self):
        import os
        os.environ["SYNK_LOCAL_MODEL"] = "endpoint"
        try:
            self.assertIsInstance(build_local_model(), EndpointLocalModel)
        finally:
            del os.environ["SYNK_LOCAL_MODEL"]


class TestRuntimeFailsafe(unittest.TestCase):
    def test_endpoint_down_escalates(self):
        rt = LocalRuntime(model=EndpointLocalModel(url="http://x/", model="t"))
        with patch("urllib.request.urlopen", side_effect=ConnectionError("down")):
            decision, routing = rt.decide("site", "sig", "do a real thing")
        self.assertEqual(routing, "cloud_llm")
        self.assertEqual(decision["decision"], "escalate")


if __name__ == "__main__":
    unittest.main()
