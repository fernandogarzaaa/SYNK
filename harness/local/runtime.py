"""LocalRuntime: The heart of Beta.2 intelligence routing.
Implements: Cache -> Deterministic -> Local SLM -> Cloud Escalation.
"""
from __future__ import annotations
import json
import time
from typing import Any, Optional, Dict, Tuple

from .model import (LocalModel, MockLocalModel, EndpointLocalModel,
                    build_local_model, InferenceRequest, InferenceResult)
from .cache import DecisionCache
from .telemetry import LocalTelemetry, DecisionMetric

class LocalRuntime:
    def __init__(self, model: Optional[LocalModel] = None):
        # SYNK_LOCAL_MODEL=endpoint selects a real local server; default mock.
        # Endpoint failures fail safe: decide() catches per-call errors below.
        self.model = model or build_local_model()
        self.cache = DecisionCache()
        self.telemetry = LocalTelemetry()
        
        # Confidence Gates
        self.CONF_HIGH = 0.90
        self.CONF_MID = 0.60

    def decide(self, site: str, state_sig: str, intent: str, 
               workflow_id: Optional[str] = None, 
               context: Optional[Dict] = None) -> Tuple[Dict[str, Any], str]:
        """
        Determines the next action using the Beta.2 routing logic.
        Returns (decision_payload, routing_type).
        """
        start_time = time.time()
        ctx = context or {}

        # 1. Cache Lookup (L-0)
        cached = self.cache.lookup(site, state_sig, intent, workflow_id)
        if cached and cached.success_rate > 0.95:
            latency = (time.time() - start_time) * 1000
            self.telemetry.record(DecisionMetric(
                type='cache', latency_ms=latency, 
                tokens_saved=50, cost_saved=0.001
            ))
            return cached.action, 'cache'

        # 2. Deterministic Check (L-0.5)
        # (In a real system, this calls classifier.py/selector.py for trivial patterns)
        if "trivial" in intent.lower():
            latency = (time.time() - start_time) * 1000
            self.telemetry.record(DecisionMetric(
                type='deterministic', latency_ms=latency,
                tokens_saved=100, cost_saved=0.002
            ))
            return {"decision": "fill", "ref": "trivial_field", "confidence": 1.0}, 'deterministic'

        # 3. Local SLM (L-1) — endpoint failures escalate, never crash the loop
        prompt = f"Site: {site}\nState: {state_sig}\nIntent: {intent}\nDecision?"
        req = InferenceRequest(prompt=prompt, context=ctx)
        try:
            res = self.model.infer(req)
        except Exception as e:
            latency = (time.time() - start_time) * 1000
            self.telemetry.record(DecisionMetric(
                type='cloud_llm', latency_ms=latency,
                was_escalated=True, confidence=0.0
            ))
            return {"decision": "escalate", "reason": f"local_model_error: {e}",
                    "confidence": 0.0}, 'cloud_llm'

        try:
            decision = json.loads(res.text)
        except json.JSONDecodeError:
            decision = {"decision": "escalate", "reason": "malformed_slm_output"}

        confidence = decision.get("confidence", 0.0)
        latency = (time.time() - start_time) * 1000

        # Confidence Routing
        if confidence >= self.CONF_HIGH:
            self.telemetry.record(DecisionMetric(
                type='local_slm', latency_ms=latency,
                tokens_saved=200, cost_saved=0.005
            ))
            return decision, 'local_slm'
        
        elif confidence >= self.CONF_MID:
            # Mid-confidence: Attempt deterministic validation (Mocked here)
            # if validate(decision): return decision, 'local_slm'
            pass

        # 4. Cloud Escalation (L-2)
        self.telemetry.record(DecisionMetric(
            type='cloud_llm', latency_ms=latency, 
            was_escalated=True, confidence=confidence
        ))
        return {"decision": "escalate", "reason": "low_confidence", "confidence": confidence}, 'cloud_llm'

    def learn(self, site: str, state_sig: str, intent: str, 
              action: Dict[str, Any], success: bool, 
              confidence: float, workflow_id: Optional[str] = None):
        """Updates the DecisionCache with the outcome of an action."""
        self.cache.update(site, state_sig, intent, action, success, confidence, workflow_id)
