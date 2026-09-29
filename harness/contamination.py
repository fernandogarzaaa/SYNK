"""Contamination guards: page-supplied strings are untrusted data (Stage F).

Every string that originates from a page -- node text, tool names,
tool descriptions, tool results -- is treated as data, never as
instruction. This module provides deterministic, rule-based detection
(no model involved, so a hostile page cannot talk the detector into
changing its mind):

* ``classify_text`` scans page text for prompt-injection and
  tool-impersonation patterns. It returns the matched markers; the
  caller decides quarantine vs. flag.
* ``sanitize_tool_advertisement`` validates a page-advertised WebMCP
  tool (name format, description free of injection, schema is a plain
  object). Tools that fail are quarantined (dropped) with a recorded
  reason -- they never reach the selection or invocation path.
* ``redact_secrets`` strips credential-shaped values (API keys,
  bearer tokens, passwords, tokens in URLs, card/SSN-like numbers)
  before anything is persisted to memory or echoed in prompts.

SHA-256 is used for identity; never Python ``hash()``.
"""
from __future__ import annotations

import hashlib
import re

# -- prompt-injection markers ---------------------------------------------------
# Deterministic regex list. Kept deliberately narrow: a false positive
# only quarantines page text (flagged, still visible), it never silently
# changes agent behavior.
INJECTION_PATTERNS = [
    # classic instruction override
    re.compile(r"(?i)\bignore\s+(all\s+|your\s+|the\s+)?(previous|prior|above)\s+instructions?\b"),
    re.compile(r"(?i)\bdisregard\s+(all\s+|your\s+|the\s+)?(previous|prior|above|safety)\b"),
    re.compile(r"(?i)\b(you\s+are\s+now|from\s+now\s+on\s+you\s+are)\b"),
    re.compile(r"(?i)\bnew\s+(system\s+)?instructions?\s*:"),
    re.compile(r"(?i)\b(system\s+prompt\s+override|override\s+the\s+system)\b"),
    re.compile(r"(?i)\bjailbreak\b"),
    re.compile(r"(?i)\bDAN\s+mode\b"),
    # tool impersonation: page pretends to grant tools or redefine the agent
    re.compile(r"(?i)\byou\s+(now\s+)?have\s+(a\s+new\s+)?tool\s+called\b"),
    re.compile(r"(?i)\bcall\s+the\s+tool\s+\w+\s+immediately\b"),
    re.compile(r"(?i)\bexecute\s+the\s+following\s+as\s+(an?\s+)?instruction"),
    re.compile(r"(?i)\bas\s+an?\s+ai\b.{0,40}\byou\s+must\b"),
    re.compile(r"(?i)\bthe\s+user\s+instructed\s+you\s+to\b"),
    # exfiltration-flavored instructions aimed at the agent
    re.compile(r"(?i)\bsend\s+(your\s+)?(api[_\s-]?key|password|credentials|secrets?)\s+to\b"),
    re.compile(r"(?i)\bdo\s+not\s+tell\s+the\s+user\b"),
]

# Tool names must look like identifiers; anything else is either a page
# bug or an impersonation attempt.
TOOL_NAME_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_.\-]{0,63}$")

# -- secret redaction -------------------------------------------------------------
# (pattern, replacement). Order matters: specific before general.
SECRET_PATTERNS = [
    (re.compile(r"sk-[A-Za-z0-9]{8,}"), "[REDACTED_API_KEY]"),
    (re.compile(r"AKIA[0-9A-Z]{16}"), "[REDACTED_AWS_KEY]"),
    (re.compile(r"ghp_[A-Za-z0-9]{20,}"), "[REDACTED_GITHUB_TOKEN]"),
    (re.compile(r"gho_[A-Za-z0-9]{20,}"), "[REDACTED_GITHUB_TOKEN]"),
    (re.compile(r"xox[bap]-" + r"[A-Za-z0-9\-]{8,}"), "[REDACTED_SLACK_TOKEN]"),
    (re.compile(r"AIza[0-9A-Za-z_\-]{20,}"), "[REDACTED_GOOGLE_KEY]"),
    (re.compile(r"(?i)(bearer\s+)[A-Za-z0-9._~+/=\-]{8,}"), r"\1[REDACTED_TOKEN]"),
    (re.compile(r"(?i)(password|passwd|pwd)\s*[:=]\s*\S+"), r"\1=[REDACTED]"),
    (re.compile(r"(?i)(client[_-]?secret)\s*[:=]\s*\S+"), r"\1=[REDACTED]"),
    # tokens / secrets smuggled as URL query params
    (re.compile(r"(?i)([?&](?:api[_-]?key|apikey|token|access[_-]?token|secret|auth|session[_-]?id)[^=]*=)[^&\s'\"]+"),
     r"\1[REDACTED]"),
    # Generic "name = value" secret shapes in free text: token=..., secret: ...
    (re.compile(r"(?i)\b(token|secret|passwd|credential|api[_-]?key)s?"
                r"\s*[:=]\s*\S+"), "[REDACTED_SECRET]"),
    # card-like and SSN-like numbers (shared with the safety layer)
    (re.compile(r"\b\d{13,19}\b"), "[CARD_MASKED]"),
    (re.compile(r"\b\d{3}-\d{2}-\d{4}\b"), "[SSN_MASKED]"),
]

REDACTED_MARKERS = ("[REDACTED", "[CARD_MASKED]", "[SSN_MASKED]")


def looks_redacted(text: str) -> bool:
    """True when redaction already replaced something in this text."""
    t = text or ""
    return any(m in t for m in REDACTED_MARKERS)


def redact_secrets(value) -> object:
    """Redact credential-shaped values inside strings (recurses into
    dicts/lists). Non-string scalars pass through unchanged."""
    if isinstance(value, dict):
        return {k: redact_secrets(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [redact_secrets(v) for v in value]
    if not isinstance(value, str):
        return value
    out = value
    for pat, repl in SECRET_PATTERNS:
        out = pat.sub(repl, out)
    return out


def classify_text(text: str) -> dict:
    """Deterministic injection scan of page-supplied text.

    Returns {"injection": bool, "markers": [pattern strings that hit]}.
    Never raises; empty/None text is clean.
    """
    markers = []
    t = text or ""
    if not t:
        return {"injection": False, "markers": markers}
    for pat in INJECTION_PATTERNS:
        m = pat.search(t)
        if m:
            markers.append(m.group(0)[:80])
            if len(markers) >= 5:
                break
    return {"injection": bool(markers), "markers": markers}


def quarantine_id(*parts: str) -> str:
    """Stable quarantine record id (SHA-256 over canonical parts)."""
    canon = "|".join(str(p) for p in parts)
    return "q_" + hashlib.sha256(canon.encode()).hexdigest()[:12]


def sanitize_tool_advertisement(tool: dict) -> tuple[dict | None, str]:
    """Validate one page-advertised WebMCP tool.

    Returns (cleaned_tool, "") on success or (None, reason) when the
    advertisement must be quarantined: malformed name, non-object
    schema, or an injection marker in the name/description. The tool
    dict is treated as untrusted data throughout.
    """
    if not isinstance(tool, dict):
        return None, "advertisement is not an object"
    name = tool.get("name")
    if not isinstance(name, str) or not TOOL_NAME_RE.match(name):
        return None, f"quarantined: tool name {name!r} is not a valid identifier"
    desc = tool.get("description", "")
    if not isinstance(desc, str):
        desc = ""
    hit = classify_text(f"{name}\n{desc}")
    if hit["injection"]:
        return None, ("quarantined: injection marker in tool advertisement "
                      f"({hit['markers'][0]!r})")
    schema = tool.get("input_schema", {})
    if schema is not None and not isinstance(schema, dict):
        return None, "quarantined: input_schema is not an object"
    cleaned = {"name": name,
               "description": desc[:2000],
               "input_schema": schema or {},
               "annotations": tool.get("annotations")
               if isinstance(tool.get("annotations"), dict) else {}}
    return cleaned, ""


def sanitize_tool_result(result) -> object:
    """Tool results are untrusted data: redact secrets, cap size, keep type.

    The result is evidence, never instruction. It is never executed,
    interpolated into a prompt verbatim, or re-dispatched.
    """
    redacted = redact_secrets(result)
    s = redacted if isinstance(redacted, str) else None
    if s is not None and len(s) > 20000:
        return s[:20000] + "…[truncated]"
    return redacted


# -- declared-schema validation ------------------------------------------------------
# Deterministic JSON-Schema-subset validator for WebMCP tool inputs.
# Anything not provably valid fails closed. Unknown keywords are ignored
# (the page's schema is advisory); unsupported-but-present constraints
# are treated as pass-through, never as proof of validity.

_JSON_TYPES = ("string", "number", "integer", "boolean", "object",
               "array", "null")


def _type_ok(value, want: str) -> bool:
    if want == "string":
        return isinstance(value, str)
    if want == "number":
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    if want == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    if want == "boolean":
        return isinstance(value, bool)
    if want == "object":
        return isinstance(value, dict)
    if want == "array":
        return isinstance(value, list)
    if want == "null":
        return value is None
    return True  # unknown type keyword: cannot disprove


def _check_value(value, schema: dict, path: str) -> str | None:
    """Return an error string, or None when the value satisfies the schema."""
    if not isinstance(schema, dict):
        return None
    want = schema.get("type")
    if want is not None:
        wants = want if isinstance(want, list) else [want]
        if not any(_type_ok(value, w) for w in wants):
            return f"{path}: must be {want}"
    if isinstance(value, dict):
        for p in (schema.get("required") or []):
            if p not in value:
                return f"{path}: missing required param '{p}'"
        props = schema.get("properties") or {}
        if isinstance(props, dict):
            for k, v in value.items():
                spec = props.get(k)
                if spec is None:
                    if schema.get("additionalProperties") is False:
                        return f"{path}: unexpected param '{k}'"
                    continue
                err = _check_value(v, spec, f"{path}.{k}")
                if err:
                    return err
    if isinstance(value, list):
        items = schema.get("items")
        if isinstance(items, dict):
            for i, v in enumerate(value):
                err = _check_value(v, items, f"{path}[{i}]")
                if err:
                    return err
    if "enum" in schema and isinstance(schema["enum"], list):
        if value not in schema["enum"]:
            return f"{path}: not one of {schema['enum']!r}"
    if isinstance(value, str):
        if schema.get("minLength") is not None and \
                len(value) < schema["minLength"]:
            return f"{path}: shorter than minLength {schema['minLength']}"
        if schema.get("maxLength") is not None and \
                len(value) > schema["maxLength"]:
            return f"{path}: longer than maxLength {schema['maxLength']}"
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        if schema.get("minimum") is not None and value < schema["minimum"]:
            return f"{path}: below minimum {schema['minimum']}"
        if schema.get("maximum") is not None and value > schema["maximum"]:
            return f"{path}: above maximum {schema['maximum']}"
    return None


def validate_input_schema(args, schema: dict) -> str | None:
    """Validate tool inputs against the tool's declared input_schema.

    Returns an error string, or None when valid. Non-object args or a
    non-object schema fail closed.
    """
    if not isinstance(args, dict):
        return "args must be an object"
    if schema is not None and not isinstance(schema, dict):
        return "tool input_schema is not an object"
    return _check_value(args, schema or {}, "args")
