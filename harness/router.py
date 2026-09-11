"""Model router: tiered inference, cheapest capable tier first (Beta §13).

  Tier 0 - deterministic rules (is user typing? did page change?)
  Tier 1 - tiny local classifier (element <-> invoice number, intent hints)
  Tier 2 - small local planning model (routine fills, suggestions)
  Tier 3 - cloud reasoning model (complex multi-step planning)
  Tier 4 - vision / specialist model (charts, canvas, captchas)

Never waste frontier inference on trivial browser state.
"""
from __future__ import annotations

import re

TIERS = ("rule", "classifier", "small-local", "cloud", "vision")

RULE_RE = re.compile(r"(?i)\b(typing|focused|page changed|diff|empty|visible)\b")
CLASSIFY_RE = re.compile(r"(?i)\b(which|label|correspond|invoice|classify|intent|match)\b")
VISION_RE = re.compile(r"(?i)\b(chart|image|captcha|canvas|screenshot|ocr|visual)\b")
COMPLEX_RE = re.compile(r"(?i)\b(plan|workflow|compare|research|multi-step|book|purchase|trip)\b")


def pick_tier(task: str, vision_available: bool = False,
              workflow_matched: bool = False) -> tuple[str, str]:
    """Return (tier, reason). Deterministic Tier 0/1, heuristic above."""
    t = task or ""
    if RULE_RE.search(t):
        return "rule", "decidable without any model (state lookup/diff)"
    if VISION_RE.search(t):
        return ("vision", "needs visual perception") if vision_available \
            else ("cloud", "vision unavailable; strongest text model + human handoff")
    if CLASSIFY_RE.search(t):
        return "classifier", "single judgment call; tiny model suffices"
    if workflow_matched:
        return "small-local", "known workflow; routine execution, no frontier needed"
    if COMPLEX_RE.search(t):
        return "cloud", "complex planning; frontier model justified"
    return "small-local", "default: routine task on small model"


def tier_to_model_label(tier: str, cloud_name: str = "gpt-4o-mini",
                        local_name: str = "mock-small-3B") -> str:
    return {"rule": "rule:deterministic",
            "classifier": f"local:{local_name}-cls",
            "small-local": f"local:{local_name}",
            "cloud": f"cloud:{cloud_name}",
            "vision": "specialist:vision"}.get(tier, f"local:{local_name}")
