"""Execution ladder: cheapest reliable layer first (Beta §7, §20, Beta.1).

  L0 - WebMCP / site-native semantic API: searchProducts(), checkout(), ...
  L1 - DOM semantic actions: click(ref), fill(ref, value), ...
  L2 - DOM event / browser primitives: keyboard, mouse, scroll, drag
  L3 - Selective vision: screenshot, OCR, canvas interpretation
  L4 - Keyboard + pointer primitives
  L5 - Human takeover: "Take over here."

Universal compatibility = graceful degradation down this ladder.
WebMCP is an acceleration path, not the compatibility mechanism.
"""
from __future__ import annotations

from typing import Any

from .webmcp import REGISTRY, Capability, WebMCPAdapter, ADAPTER


class SiteRegistry:
    """Level-0 adapters. Sites register structured tools via WebMCP Registry."""

    def __init__(self):
        # Legacy: direct tool callables (for non-WebMCP adapters)
        self.adapters: dict[str, dict] = {}

    def register(self, domain: str, tools: dict) -> None:
        self.adapters[domain] = tools

    def get(self, domain: str, tool: str) -> Any:
        return self.adapters.get(domain, {}).get(tool)

    def has_domain(self, domain: str) -> bool:
        return domain in self.adapters


class ExecutionLadder:
    """Chooses the cheapest reliable execution level for a goal."""

    def __init__(self, registry: SiteRegistry | None = None,
                 webmcp_adapter: WebMCPAdapter | None = None):
        self.registry = registry or SiteRegistry()
        self.webmcp = webmcp_adapter or ADAPTER

    def choose_level(self, domain: str, goal: str,
                     vision_available: bool = False,
                     user_consented: bool = False) -> tuple[int, str, Capability | None]:
        """
        Returns (level, reason, capability).
        Level 0 = WebMCP capability found and policy allows.
        """
        g = (goal or "").lower()

        # L0: WebMCP semantic API
        cap = self.webmcp.choose_best_capability(goal, domain)
        if cap and cap.source == "webmcp":
            # Check policy allows
            from .webmcp.policy import CAPABILITY_POLICY
            if CAPABILITY_POLICY.is_allowed(cap):
                # Check consent for risky operations
                if cap.requires_user_consent and not user_consented:
                    pass  # fall through to lower level
                else:
                    return 0, f"WebMCP: {cap.name} (risk={cap.risk})", cap

        # L1: DOM semantic actions
        if any(w in g for w in ("click", "fill", "type", "select", "form", "search")):
            return 1, "DOM semantic actions", None

        # L2: DOM event / browser primitives
        if any(w in g for w in ("scroll", "drag", "hover", "press", "keyboard")):
            return 2, "browser primitives", None

        # L3: Selective vision
        if any(w in g for w in ("read chart", "captcha", "canvas", "image", "ocr", "visual")):
            return (3, "selective vision", None) if vision_available else (5, "human handoff (vision unavailable)", None)

        # L4: Keyboard + pointer primitives
        return 4, "keyboard/pointer primitives", None

    def execute_webmcp(self, domain: str, cap: Capability, args: dict,
                       goal: str = "", user_consented: bool = False) -> dict:
        """Execute a WebMCP capability through the adapter."""
        return self.webmcp.execute(domain, cap.name, args, goal, user_consented)

    def describe(self) -> list[dict]:
        webmcp_sites = []
        for origin in REGISTRY._by_origin:
            caps = REGISTRY.get_for_origin(origin)
            webmcp_tools = [c.name for c in caps if c.source == "webmcp"]
            if webmcp_tools:
                webmcp_sites.append({"origin": origin, "tools": webmcp_tools})

        return [
            {"level": 0, "name": "WebMCP / semantic API", "sites": webmcp_sites},
            {"level": 1, "name": "DOM semantic actions"},
            {"level": 2, "name": "browser primitives"},
            {"level": 3, "name": "selective vision"},
            {"level": 4, "name": "keyboard/pointer primitives"},
            {"level": 5, "name": "human handoff"},
        ]


# Convenience: global ladder with WebMCP integration
LADDER = ExecutionLadder(webmcp_adapter=ADAPTER)
