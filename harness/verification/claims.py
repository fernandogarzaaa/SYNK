"""Claims: Proposed outcomes that require verification (Beta.2 Truth Layer).
Agents propose claims; the runtime verifies them.
"""
from __future__ import annotations
from dataclasses import dataclass, field
from typing import Any, Optional, Dict, List
import time
import uuid

# Claim Types
CLAIM_TYPE_STATE = "STATE_CHANGE"
CLAIM_TYPE_ACTION = "ACTION_COMPLETED"
CLAIM_TYPE_OUTCOME = "OUTCOME_REACHED"

@dataclass
class Claim:
    claim_id: str
    task_id: str
    actor: str # 'agent' | 'human' | 'system'
    claim_type: str
    target: str
    requested_state: Any
    claimed_state: Any
    timestamp: float = field(default_factory=time.time)
    action_ids: List[str] = field(default_factory=list)
    status: str = "proposed" # proposed | verified | failed | unverified | conflicting
    # Stage C: the tool that produced this claim and its declared
    # postcondition, e.g. {"kind": "element_value", "target": "#email",
    # "value": "a@b.c"}. When set, the verifier only accepts evidence
    # appropriate to the postcondition kind (see evidence.satisfies_...).
    tool: str = ""
    postcondition: Optional[Dict[str, Any]] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "claim_id": self.claim_id,
            "task_id": self.task_id,
            "actor": self.actor,
            "claim_type": self.claim_type,
            "target": self.target,
            "requested_state": self.requested_state,
            "claimed_state": self.claimed_state,
            "timestamp": self.timestamp,
            "action_ids": self.action_ids,
            "status": self.status,
            "tool": self.tool,
            "postcondition": self.postcondition,
        }
