"""Verification results and outcomes (Beta.2 Truth Layer)."""
from __future__ import annotations
from dataclasses import dataclass, field
from typing import Any, Optional, List, Dict

# Verification Result States
VERIFIED = "VERIFIED"
FAILED = "FAILED"
UNVERIFIED = "UNVERIFIED"
CONFLICTING = "CONFLICTING"

@dataclass
class VerificationResult:
    result: str # VERIFIED | FAILED | UNVERIFIED | CONFLICTING
    evidence_ids: List[str] = field(default_factory=list)
    reason: str = ""
    confidence: float = 0.0
    timestamp: float = 0.0

    def to_dict(self) -> Dict[str, Any]:
        return {
            "result": self.result,
            "evidence_ids": self.evidence_ids,
            "reason": self.reason,
            "confidence": self.confidence,
            "timestamp": self.timestamp,
        }
