"""Independent Verifier: The ground truth authority for agent claims (Beta.2 Truth Layer).
Implements INV-08 to INV-17: verification must be independent of the acting model.
"""
from __future__ import annotations
import time
from typing import List, Dict

from .evidence import (Evidence, DOM_CHANGE, NAVIGATION, URL_CHANGE,
                         WEBMCP_RESULT, BROWSER_EVENT,
                         satisfies_postcondition)
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
        # 1. WebMCP Results. Fixture-fallback evidence (partial_fallback)
        #    is excluded here exactly as in the postcondition path: a
        #    fixture result can never verify or fail a claim -- it proves
        #    nothing about the live page.
        webmcp_ev = [e for e in relevant_evidence
                     if e.evidence_type == WEBMCP_RESULT
                     and not (e.payload or {}).get("partial_fallback")]
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

        # 4. Stage C: postcondition-appropriate evidence. When the claim
        # declares a postcondition, ONLY evidence types appropriate to that
        # postcondition kind may verify it. BROWSER_EVENT ("the executor
        # accepted the command"), bare screenshots, and unrelated evidence
        # can never satisfy a postcondition -- this is the central
        # invariant: VERIFIED requires independent observation.
        if claim.postcondition:
            return self._verify_postcondition(claim, relevant_evidence)

        # 5. Final decision: require a positive signal, never auto-verify on presence alone
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

    # -- Stage C: postcondition-gated verification --------------------------------
    def _verify_postcondition(self, claim: Claim,
                              relevant: list) -> VerificationResult:
        """Verify a claim against its declared postcondition.

        Only evidence appropriate to the postcondition kind counts
        (evidence.satisfies_postcondition). Unknown postcondition kinds fail
        closed as UNVERIFIED.
        """
        pc = claim.postcondition or {}
        kind = pc.get("kind")
        if kind not in ("element_value", "element_interaction", "url",
                        "webmcp_result"):
            return VerificationResult(
                UNVERIFIED,
                reason=f"unknown postcondition kind {kind!r}; failing closed",
                timestamp=time.time())
        appropriate = [e for e in relevant
                       if satisfies_postcondition(e.evidence_type, kind,
                                                  e.payload)]
        if not appropriate:
            have = sorted({e.evidence_type for e in relevant})
            return VerificationResult(
                UNVERIFIED,
                [e.evidence_id for e in relevant],
                reason=("no postcondition-appropriate evidence for "
                        f"{kind}; have only: {have or 'none'} "
                        "(command-accepted evidence is never sufficient)"),
                timestamp=time.time())
        ids = [e.evidence_id for e in appropriate]

        def _target_matches(e) -> bool:
            want = pc.get("target")
            got = (e.payload or {}).get("target")
            return not want or not got or str(got) == str(want)

        if kind == "url":
            want = pc.get("url")
            for e in appropriate:
                if _target_matches(e) and e.payload.get("url") == want:
                    return VerificationResult(
                        VERIFIED, ids,
                        reason=f"independent URL observation matches {want}",
                        confidence=0.9, timestamp=time.time())
            return VerificationResult(
                UNVERIFIED, ids,
                reason="URL observations do not match postcondition",
                timestamp=time.time())

        if kind == "element_value":
            want = pc.get("value")
            for e in appropriate:
                if not _target_matches(e):
                    continue
                p = e.payload or {}
                actual = p.get("value", p.get("state",
                             p.get("text", p.get("detail"))))
                if actual is not None and str(actual) == str(want):
                    return VerificationResult(
                        VERIFIED, ids,
                        reason=(f"independent observation confirms "
                                f"{pc.get('target')} == {want!r}"),
                        confidence=0.9, timestamp=time.time())
            return VerificationResult(
                UNVERIFIED, ids,
                reason=("appropriate evidence present but no observation "
                        "confirms the expected value"),
                timestamp=time.time())

        # webmcp_result (Stage E): the page's own tool report. Checked
        # BEFORE the generic interaction branch so fixture-fallback
        # evidence (which shares the WEBMCP_RESULT type) can never
        # verify through the back door.
        if kind == "webmcp_result":
            return self._verify_webmcp_result(pc, appropriate, ids)

        # element_interaction: appropriate independent evidence (a DOM/state
        # observation, navigation, or application/human confirmation tied to
        # this claim) is sufficient.
        matched = [e for e in appropriate if _target_matches(e)]
        if matched:
            kinds = sorted({e.evidence_type for e in matched})
            return VerificationResult(
                VERIFIED, [e.evidence_id for e in matched],
                reason=f"independent observation confirms interaction "
                       f"({', '.join(kinds)})",
                confidence=0.85, timestamp=time.time())
        return VerificationResult(
            UNVERIFIED, ids,
            reason="appropriate evidence present but not tied to the target",
            timestamp=time.time())

    @staticmethod
    def _verify_webmcp_result(pc: dict, appropriate: list,
                              ids: list) -> VerificationResult:
        """Verify a WebMCP invocation claim (Stage E).

        The page's own model-context tool result is the independent
        observation: the page reported its own tool outcome. Rules, fail
        closed:

        * the evidence must name the claimed tool (payload["tool"]);
        * a fixture-fallback result (payload["partial_fallback"] is True)
          can NEVER verify -- it proves nothing about the live page;
        * ok=True with no contradiction -> VERIFIED;
        * ok=False -> FAILED (the tool ran and reported failure -- this is
          a verification failure, not an execution failure);
        * no page-reported result at all -> UNVERIFIED.
        """
        want_tool = pc.get("tool_name")
        real = [e for e in appropriate
                if not (e.payload or {}).get("partial_fallback")]
        if want_tool:
            real = [e for e in real
                    if (e.payload or {}).get("tool") == want_tool]
        if not real:
            return VerificationResult(
                UNVERIFIED, ids,
                reason=(f"no page-reported tool result for "
                        f"'{want_tool or '?'}' (fixture results excluded)"),
                timestamp=time.time())
        last = real[-1]
        payload = last.payload or {}
        if payload.get("ok") is True:
            return VerificationResult(
                VERIFIED, [e.evidence_id for e in real],
                reason=(f"page model-context tool '{want_tool}' reported "
                        f"success"),
                confidence=0.9, timestamp=time.time())
        return VerificationResult(
            FAILED, [last.evidence_id],
            reason=(f"tool '{want_tool}' reported failure: "
                    f"{payload.get('error') or 'unknown error'}"),
            confidence=1.0, timestamp=time.time())

    def get_evidence_for_task(self, task_id: str) -> List[Evidence]:
        return [e for e in self.evidence_store.values() if e.task_id == task_id]
