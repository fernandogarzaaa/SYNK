"""LocalModel: Abstract interface for on-device SLM inference (Beta.2).
Prevents coupling the harness to a specific framework (WebLLM, MLC-LLM, etc).
"""
from __future__ import annotations
from dataclasses import dataclass
from typing import Any, Optional, Dict

@dataclass
class InferenceRequest:
    prompt: str
    context: Dict[str, Any]
    max_tokens: int = 128
    temperature: float = 0.0

@dataclass
class InferenceResult:
    text: str
    confidence: float
    latency_ms: float
    tokens_used: int
    provider: str

class LocalModel:
    """Base class for all local model adapters."""
    def infer(self, request: InferenceRequest) -> InferenceResult:
        raise NotImplementedError("Subclasses must implement infer()")

class MockLocalModel(LocalModel):
    """
    Deterministic mock SLM for Beta.2 development.
    Simulates confidence-based routing and decision patterns.
    """
    def infer(self, request: InferenceRequest) -> InferenceResult:
        # Mock behavior: if 'escalate' in prompt, simulate low confidence
        # Otherwise simulate a high-confidence structured decision
        confidence = 0.95
        text = ('{"decision": "fill", "ref": 1, '
                '"text": "<value for field 1>", "confidence": 0.95}')
        
        if "escalate" in request.prompt.lower():
            confidence = 0.45
            text = '{"decision": "escalate", "reason": "ambiguous_target", "confidence": 0.45}'
        
        return InferenceResult(
            text=text,
            confidence=confidence,
            latency_ms=12.5, # Local SLMs are fast
            tokens_used=15,
            provider="mock-slm-3b"
        )
