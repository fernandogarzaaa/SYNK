import pytest
import time
import uuid
from harness.verification.verifier import Verifier
from harness.verification.claims import Claim, CLAIM_TYPE_STATE
from harness.verification.evidence import Evidence, WEBMCP_RESULT, URL_CHANGE, DOM_CHANGE
from harness.verification.results import VERIFIED, FAILED, UNVERIFIED, CONFLICTING

class MockWorldState:
    def __init__(self):
        self.state = {}

@pytest.fixture
def verifier():
    return Verifier(MockWorldState())

def test_hallucinated_success_contradicted_by_url(verifier):
    """
    Scenario: Agent claims they navigated to /dashboard, but the runtime 
    observes they are still on /login.
    """
    task_id = "task_1"
    action_id = "action_1"
    
    # 1. Agent proposes a claim
    claim = Claim(
        claim_id="c1",
        task_id=task_id,
        actor="agent",
        claim_type=CLAIM_TYPE_STATE,
        target="url",
        requested_state="/dashboard",
        claimed_state="/dashboard",
        action_ids=[action_id]
    )
    verifier.propose_claim(claim)
    
    # 2. Runtime observes a contradictory URL
    evidence = Evidence(
        evidence_id="e1",
        evidence_type=URL_CHANGE,
        source="runtime",
        timestamp=time.time(),
        action_id=action_id,
        task_id=task_id,
        payload={"url": "/login"}
    )
    verifier.record_evidence(evidence)
    
    result = verifier.verify("c1")
    assert result.result == CONFLICTING
    assert "contradicts claim" in result.reason

def test_hallucinated_success_with_failed_webmcp(verifier):
    """
    Scenario: Agent claims success, but the underlying WebMCP tool 
    reported a failure.
    """
    task_id = "task_2"
    action_id = "action_2"
    
    claim = Claim(
        claim_id="c2",
        task_id=task_id,
        actor="agent",
        claim_type=CLAIM_TYPE_STATE,
        target="button_click",
        requested_state="clicked",
        claimed_state="clicked",
        action_ids=[action_id]
    )
    verifier.propose_claim(claim)
    
    evidence = Evidence(
        evidence_id="e2",
        evidence_type=WEBMCP_RESULT,
        source="webmcp",
        timestamp=time.time(),
        action_id=action_id,
        task_id=task_id,
        payload={"ok": False, "error": "Element not found"}
    )
    verifier.record_evidence(evidence)
    
    result = verifier.verify("c2")
    assert result.result == FAILED
    assert "WebMCP reported failure" in result.reason

def test_claim_with_no_evidence(verifier):
    """
    Scenario: Agent claims success, but the runtime observed nothing.
    """
    claim = Claim(
        claim_id="c3",
        task_id="task_3",
        actor="agent",
        claim_type=CLAIM_TYPE_STATE,
        target="text_input",
        requested_state="filled",
        claimed_state="filled",
        action_ids=["action_3"]
    )
    verifier.propose_claim(claim)
    
    # No evidence recorded
    
    result = verifier.verify("c3")
    assert result.result == UNVERIFIED
    assert "No relevant evidence observed" in result.reason

def test_timing_window_expiration(verifier):
    """
    Scenario: Evidence exists but it's too old (outside the 10s window).
    """
    task_id = "task_4"
    action_id = "action_4"
    
    # Claim happened now
    claim = Claim(
        claim_id="c4",
        task_id=task_id,
        actor="agent",
        claim_type=CLAIM_TYPE_STATE,
        target="url",
        requested_state="/home",
        claimed_state="/home",
        action_ids=[action_id]
    )
    verifier.propose_claim(claim)
    
    # Evidence happened 15 seconds ago
    evidence = Evidence(
        evidence_id="e4",
        evidence_type=URL_CHANGE,
        source="runtime",
        timestamp=time.time() - 15,
        action_id=action_id,
        task_id=task_id,
        payload={"url": "/home"}
    )
    verifier.record_evidence(evidence)
    
    result = verifier.verify("c4")
    assert result.result == UNVERIFIED # Should be unverified because evidence is too old
