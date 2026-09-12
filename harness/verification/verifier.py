"""Independent Verifier: The ground truth authority for agent claims (Beta.2 Truth Layer).
Implements INV-08 to INV-17: verification must be independent of the acting model.
"""
from __future__ import annotations
import time
from typing import Any, Optional, List, Dict, Tuple

from .evidence import Evidence, DOM_CHANGE, NAVIGATION, URL_CHANGE, WEBMCP_RESULT, BROWSER_EVENT
from .claims import Claim
from .results import VerificationResult, VERIFIED, FAILED, UNVERIFIED, CONFLICTING

class Verifier:
    def __init__(self, world_state):
        self.world = world_state
        self.evidence_store: Dict[str, Evidence] = {}
        self.claims_store: Dict[str, Claim] = {}

    def record_evidence(self, evidence: Evidence):
        """Immutable record of a runtime observation."""
        self.evidence_store[evidence.evidence_id] = evidence

    def propose_claim(self, claim: Claim):
        """Agent proposes a state change happened."""
        self.claims_store[claim.claim_id] = claim

    def verify(self, claim_id: str) -> VerificationResult:
        """
        Independent verification of a claim using deterministic evidence.
        Implements a priority-based evidence check.
        """
        claim = self.claims_store.get(claim_id)
        if not claim:
            return VerificationResult(FAILED, reason="Claim not found")

        # Filter evidence relevant to this claim's target and timeframe
        relevant_evidence = [
            e for e in self.evidence_store.values()
            if (e.action_id in claim.action_ids or e.task_id == claim.task_id)
            and e.timestamp >= claim.timestamp - 10.0 # 10s window
        ]

        if not relevant_evidence:
            return VerificationResult(UNVERIFIED, reason="No relevant evidence observed")

        # Deterministic evidence check (Preferred Order)
        # 1. WebMCP Results
        webmcp_ev = [e for e in relevant_evidence if e.evidence_type == WEBMCP_RESULT]
        if webmcp_ev:
            # Check the last result
            res = webmcp_ev[-1].payload.get("ok", False)
            if not res:
                return VerificationResult(FAILED, [webmcp_ev[-1].evidence_id], 
                                         reason="WebMCP reported failure", confidence=1.0)
            # If WebMCP says OK, it's strong evidence, but we still check for contradictions
            
        # 2. URL/Navigation Changes — only when the claim is about a URL.
        # (A field-text claim like a typed value must never conflict with URL
        # evidence; compare like with like.)
        url_ev = [e for e in relevant_evidence if e.evidence_type in (URL_CHANGE, NAVIGATION)]
        if url_ev:
            last_url = url_ev[-1].payload.get("url")
            claimed = claim.claimed_state
            claimed_is_url = (
                isinstance(claimed, str) and claimed
                and (claimed.startswith(("http://", "https://",
                                         "about:", "chrome:", "/")))
            )
            if (claimed_is_url
                    and isinstance(last_url, str) and last_url
                    and last_url != claimed):
                return VerificationResult(CONFLICTING, [url_ev[-1].evidence_id],
                                         reason=f"Actual URL {last_url} contradicts claim {claimed}")

        # 3. DOM/State changes — must match claimed_state when claim is specific
        dom_ev = [e for e in relevant_evidence if e.evidence_type == DOM_CHANGE]
        if dom_ev:
            last = dom_ev[-1].payload
            actual = last.get("state", last.get("text", last.get("value")))
            claimed = claim.claimed_state
            # Only enforce when both sides are concrete scalars (avoid false conflicts)
            if (isinstance(claimed, (str, int, float, bool)) and claimed not in ("", None)
                    and isinstance(actual, (str, int, float, bool)) and actual not in ("", None)):
                if str(actual) != str(claimed):
                    return VerificationResult(CONFLICTING, [dom_ev[-1].evidence_id],
                                              reason=f"DOM state {actual!r} contradicts claim {claimed!r}")

        # 4. Final decision: require a positive signal, never auto-verify on presence alone
        if webmcp_ev and not url_ev and not dom_ev:
            # WebMCP ok=True with no contradiction is sufficient
            return VerificationResult(VERIFIED,
                                      [e.evidence_id for e in relevant_evidence],
                                      reason="WebMCP ok with no contradictions",
                                      confidence=0.9,
                                      timestamp=time.time())
        if (url_ev or dom_ev) and claim.claimed_state:
            # URL/DOM present but did not contradict above; check explicit match
            matched = False
            if url_ev and url_ev[-1].payload.get("url") == claim.claimed_state:
                matched = True
            if dom_ev:
                last = dom_ev[-1].payload
                actual = last.get("state", last.get("text", last.get("value")))
                if actual is not None and str(actual) == str(claim.claimed_state):
                    matched = True
            if matched:
                return VerificationResult(VERIFIED,
                                          [e.evidence_id for e in relevant_evidence],
                                          reason="Evidence matches claimed_state",
                                          confidence=0.9,
                                          timestamp=time.time())
            return VerificationResult(UNVERIFIED,
                                      reason="Evidence present but does not match claimed_state")

        return VerificationResult(UNVERIFIED, reason="Insufficient evidence to verify")

    def get_evidence_for_task(self, task_id: str) -> List[Evidence]:
        return [e for e in self.evidence_store.values() if e.task_id == task_id]
