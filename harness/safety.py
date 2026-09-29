"""Safety layer: deterministic, programmatic constraints (spec section 10).

The LLM never polices itself. All actions pass through this validator.
- Tool allowlist (capability-based)
- Destructive actions blocked unless explicit user consent flag
- Domain allowlist / least-privilege navigation
- PII masking for prompts/logs
- Prompt-injection tagging: page text is untrusted, can never override system goal
- Audit log with hash chain (tamper-evident)
"""
from __future__ import annotations

import hashlib
import re
import time
from dataclasses import dataclass, field

ALLOWED_TOOLS = frozenset({
    "click", "type", "select", "scroll", "navigate", "back", "forward",
    "hover", "focus", "press_key", "upload", "snapshot", "bulk",
    "summarize", "ask_user",
    # Stage E: WebMCP tool invocation through the page's model context.
    # Risk/consent is enforced per-tool by the WebMCPGateway policy check;
    # the safety layer still applies its destructive-pattern scan.
    "webmcp_invoke",
})

# Destructive verbs that always require explicit consent, even if tool is allowed.
DESTRUCTIVE_PATTERNS = re.compile(
    r"\b(delete|refund|transfer|close[_ -]?account|wipe|cancel[_ -]?order|"
    r"send(ing)?\s+(email|money|payment)|confirm[_ -]?purchase|submit[_ -]?payment)\b",
    re.IGNORECASE,
)

PII_PATTERNS = [
    (re.compile(r"\b\d{13,19}\b"), "[CARD_MASKED]"),                      # card-like
    (re.compile(r"\b\d{3}-\d{2}-\d{4}\b"), "[SSN_MASKED]"),               # SSN
    (re.compile(r"(?i)(password\s*[:=]\s*)\S+"), r"\1[MASKED]"),
    (re.compile(r"(?i)(api[_-]?key\s*[:=]\s*)\S+"), r"\1[MASKED]"),
]

INJECTION_MARKERS = re.compile(
    r"(?i)\b(ignore (previous|all|your) instructions|system prompt|"
    r"you are now|disregard (above|safety)|jailbreak|DAN mode)\b"
)


@dataclass
class SafetyConfig:
    allowed_tools: frozenset = field(default_factory=lambda: ALLOWED_TOOLS)
    allowed_domains: list = field(default_factory=list)  # empty = any domain allowed
    consent_for_destructive: bool = True
    banking_view_only: tuple = ("bank", "payroll", "wallet")


@dataclass
class AuditEntry:
    seq: int
    ts: float
    action: dict
    decision: str
    prev_hash: str
    hash: str


# Result codes
ALLOWED = "allowed"
DENIED = "denied"
CONSENT_REQUIRED = "consent_required"

class SafetyLayer:

    """Stateless validator + stateful tamper-evident audit log."""

    def __init__(self, config: SafetyConfig | None = None):
        self.config = config or SafetyConfig()
        self._audit: list[AuditEntry] = []
        self._last_hash = "GENESIS"

    # -- prompt hygiene -----------------------------------------------------
    @staticmethod
    def tag_untrusted(page_text: str) -> str:
        """Wrap page content so the LLM prompt can separate user goal from page data."""
        return f"<untrusted_page_content>\n{page_text}\n</untrusted_page_content>"

    @staticmethod
    def detect_injection(page_text: str) -> bool:
        return bool(INJECTION_MARKERS.search(page_text or ""))

    @staticmethod
    def mask_pii(text: str) -> str:
        out = text or ""
        for pat, repl in PII_PATTERNS:
            out = pat.sub(repl, out)
        return out

    # -- action validation --------------------------------------------------
    def validate(self, action: dict, page_url: str = "", user_consented: bool = False,
                 paused_for_user: bool = False) -> tuple[bool, str]:
        """Returns (ok, reason). Human priority: if paused_for_user, deny agent acts."""
        if paused_for_user:
            return False, f"{DENIED}: human is interacting - agent paused (human priority)"
        tool = action.get("tool", action.get("action", ""))
        if tool not in self.config.allowed_tools:
            return False, f"{DENIED}: tool '{tool}' not in allowlist"
        blob = f"{tool} {action.get('args', action)}"
        if self.config.consent_for_destructive and DESTRUCTIVE_PATTERNS.search(blob):
            if not user_consented:
                return False, CONSENT_REQUIRED
        if tool == "navigate":
            url = str(action.get("url", action.get("args", "")))
            if self.config.allowed_domains and not any(
                    d in url for d in self.config.allowed_domains):
                return False, f"{DENIED}: navigation outside allowlist ({url})"
            if not url.startswith(("http://", "https://", "about:", "chrome:")):
                return False, f"{DENIED}: unsafe navigation scheme ({url})"
        # Banking view-only policy example
        if any(k in (page_url or "").lower() for k in self.config.banking_view_only):
            if tool in ("click", "type", "bulk", "upload") and DESTRUCTIVE_PATTERNS.search(blob):
                if not user_consented:
                    return False, CONSENT_REQUIRED
        return True, ALLOWED


    def validate_bulk(self, actions: list[dict], **kw) -> tuple[list[dict], list[dict]]:
        allowed, denied = [], []
        for a in actions:
            ok, reason = self.validate(a, **kw)
            (allowed if ok else denied).append({**a, "_safety": reason})
        return allowed, denied

    # -- audit ---------------------------------------------------------------
    def log(self, action: dict, decision: str) -> AuditEntry:
        seq = len(self._audit)
        ts = time.time()
        payload = f"{seq}|{ts}|{action!r}|{decision}|{self._last_hash}"
        h = hashlib.sha256(payload.encode()).hexdigest()
        entry = AuditEntry(seq, ts, dict(action), decision, self._last_hash, h)
        self._audit.append(entry)
        self._last_hash = h
        return entry

    def audit_trail(self) -> list[dict]:
        return [e.__dict__ for e in self._audit]

    def verify_chain(self) -> bool:
        prev = "GENESIS"
        for e in self._audit:
            payload = f"{e.seq}|{e.ts}|{e.action!r}|{e.decision}|{prev}"
            if hashlib.sha256(payload.encode()).hexdigest() != e.hash:
                return False
            prev = e.hash
        return True
