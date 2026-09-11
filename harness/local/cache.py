"""DecisionCache: Fast-path state-to-action mapping (Beta.2).
Allows skipping LLM/SLM entirely for repeated semantic states.
"""
from __future__ import annotations
from dataclasses import dataclass, field
import time
from typing import Any, Optional, Dict, Tuple

@dataclass
class CacheEntry:
    action: Dict[str, Any]
    confidence: float
    success_rate: float = 1.0
    observations: int = 1
    last_used: float = field(default_factory=time.time)

class DecisionCache:
    def __init__(self):
        # Key: (site, state_signature, intent, workflow_id)
        self._store: Dict[Tuple[str, str, str, Optional[str]], CacheEntry] = {}

    def _get_sig(self, site: str, state_sig: str, intent: str, workflow_id: Optional[str] = None) -> Tuple:
        return (site, state_sig, intent, workflow_id)

    def lookup(self, site: str, state_sig: str, intent: str, workflow_id: Optional[str] = None) -> Optional[CacheEntry]:
        key = self._get_sig(site, state_sig, intent, workflow_id)
        entry = self._store.get(key)
        if entry:
            entry.last_used = time.time()
        return entry

    def update(self, site: str, state_sig: str, intent: str, action: Dict[str, Any], 
               success: bool, confidence: float, workflow_id: Optional[str] = None):
        key = self._get_sig(site, state_sig, intent, workflow_id)
        if key in self._store:
            entry = self._store[key]
            entry.observations += 1
            # Moving average for success rate
            entry.success_rate = (entry.success_rate * (entry.observations - 1) + (1.0 if success else 0.0)) / entry.observations
            entry.last_used = time.time()
        else:
            self._store[key] = CacheEntry(action=action, confidence=confidence)

    def clear(self):
        self._store.clear()
