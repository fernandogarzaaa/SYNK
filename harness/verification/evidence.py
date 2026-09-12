"""Evidence: Structured observations from the browser runtime (Beta.2 Truth Layer).
Evidence is immutable and serves as the ground truth for verification.
"""
from __future__ import annotations
from dataclasses import dataclass, field
from typing import Any, Optional, Dict
import time
import uuid

# Evidence Types
SCREENSHOT = "SCREENSHOT"
DOM_CHANGE = "DOM_CHANGE"
ACCESSIBILITY_CHANGE = "ACCESSIBILITY_CHANGE"
NAVIGATION = "NAVIGATION"
URL_CHANGE = "URL_CHANGE"
NETWORK_REQUEST = "NETWORK_REQUEST"
NETWORK_RESPONSE = "NETWORK_RESPONSE"
WEBMCP_RESULT = "WEBMCP_RESULT"
FORM_VALIDATION = "FORM_VALIDATION"
ELEMENT_STATE = "ELEMENT_STATE"
DOWNLOAD = "DOWNLOAD"
DIALOG = "DIALOG"
APPLICATION_CONFIRMATION = "APPLICATION_CONFIRMATION"
BROWSER_EVENT = "BROWSER_EVENT"
HUMAN_EVENT = "HUMAN_EVENT"
AGENT_EVENT = "AGENT_EVENT"

@dataclass(frozen=True)
class Evidence:
    evidence_id: str
    evidence_type: str
    source: str  # 'runtime', 'network', 'webmcp', 'human', 'agent'
    timestamp: float
    action_id: Optional[str] = None
    task_id: Optional[str] = None
    world_state_version: Optional[int] = None
    origin: Optional[str] = None
    strength: float = 1.0  # 0.0 to 1.0
    payload: Dict[str, Any] = field(default_factory=dict)
    provenance: str = "direct_observation"

    def to_dict(self) -> Dict[str, Any]:
        return {
            "evidence_id": self.evidence_id,
            "evidence_type": self.evidence_type,
            "source": self.source,
            "timestamp": self.timestamp,
            "action_id": self.action_id,
            "task_id": self.task_id,
            "world_state_version": self.world_state_version,
            "origin": self.origin,
            "strength": self.strength,
            "payload": self.payload,
            "provenance": self.provenance,
        }
