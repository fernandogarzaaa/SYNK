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

# Evidence strength hierarchy (Stage C / mandate Phase 12).
#
# A claim is VERIFIED only from evidence *appropriate to its declared
# postcondition*. Strength is provenance-weighted: "the executor accepted
# the command" (BROWSER_EVENT / COMMAND_ACCEPTED tier) is the weakest
# tier and can NEVER satisfy a postcondition on its own. A bare screenshot
# is likewise insufficient unless a dedicated vision verifier explicitly
# established the postcondition (payload["vision_verified"] is True).
EVIDENCE_STRENGTH = {
    BROWSER_EVENT: 0.10,            # COMMAND_ACCEPTED: dispatch only
    AGENT_EVENT: 0.15,              # agent-internal note
    SCREENSHOT: 0.20,               # visual; needs vision verifier
    DOM_CHANGE: 0.45,               # DOM_OBSERVATION
    ELEMENT_STATE: 0.50,            # canonical state read
    ACCESSIBILITY_CHANGE: 0.55,     # ACCESSIBILITY_OBSERVATION
    URL_CHANGE: 0.60,
    DIALOG: 0.60,
    FORM_VALIDATION: 0.60,
    NAVIGATION: 0.65,
    NETWORK_REQUEST: 0.70,          # NETWORK_RESULT tier
    NETWORK_RESPONSE: 0.72,
    WEBMCP_RESULT: 0.78,            # tool result from the page itself
    DOWNLOAD: 0.80,                 # download event + file metadata
    APPLICATION_CONFIRMATION: 0.88,  # app-level confirmation (order id, etc.)
    HUMAN_EVENT: 0.95,              # HUMAN_CONFIRMATION
}

# Postcondition kind -> evidence types that may satisfy it. Types absent
# here (BROWSER_EVENT, AGENT_EVENT, bare SCREENSHOT) can never verify a
# postcondition, no matter how many are recorded.
POSTCONDITION_EVIDENCE = {
    "element_value": {DOM_CHANGE, ACCESSIBILITY_CHANGE, ELEMENT_STATE,
                      HUMAN_EVENT, APPLICATION_CONFIRMATION},
    "element_interaction": {DOM_CHANGE, ACCESSIBILITY_CHANGE, ELEMENT_STATE,
                            URL_CHANGE, NAVIGATION, APPLICATION_CONFIRMATION,
                            HUMAN_EVENT, DIALOG},
    "url": {URL_CHANGE, NAVIGATION, HUMAN_EVENT},
}


def strength_of(evidence_type: str, payload: dict | None = None) -> float:
    """Strength of an evidence type; screenshots need a vision verifier."""
    if evidence_type == SCREENSHOT:
        if payload and payload.get("vision_verified") is True:
            return 0.85
        return EVIDENCE_STRENGTH[SCREENSHOT]
    return EVIDENCE_STRENGTH.get(evidence_type, 0.0)


def satisfies_postcondition(evidence_type: str, postcondition_kind: str,
                            payload: dict | None = None) -> bool:
    """True when this evidence type may verify this postcondition kind."""
    if evidence_type == SCREENSHOT:
        return bool(payload and payload.get("vision_verified") is True)
    allowed = POSTCONDITION_EVIDENCE.get(postcondition_kind, set())
    return evidence_type in allowed

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
