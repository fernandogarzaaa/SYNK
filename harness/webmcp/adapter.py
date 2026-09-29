"""WebMCP Adapter: LEGACY direct-execution path (pre-Stage E).

Stage E introduced the real WebMCPGateway (harness/webmcp/gateway.py),
which discovers tools from the live page's model context per tab/session.
This adapter is preserved for two honest uses only:

* the legacy ``/webmcp/execute`` endpoint (responses are labeled PARTIAL),
* the benchmark harness (``benchmark/agents.py``), which measures the
  ladder's WebMCP fast path against fixtures.

Its ``_execute_tool`` answers from the built-in fixture table -- it never
touches a page. Do not use it for production agent execution; route
through ``WebMCPGateway`` instead.
"""
from __future__ import annotations

import uuid
from typing import Any

from .schema import ToolResult, WebMCPSite
from .discovery import WebMCPDiscovery
from .fallback import LEGACY_FALLBACK_TABLE
from .registry import REGISTRY, Capability
from .policy import PolicyEngine, CAPABILITY_POLICY
from ..concurrency import LeaseManager, OwnershipGraph
from ..safety import SafetyLayer


class WebMCPAdapter:
    """Adapter that wraps WebMCP tool execution with full safety pipeline."""

    def __init__(self, discovery: WebMCPDiscovery | None = None,
                 ownership: OwnershipGraph | None = None,
                 leases: LeaseManager | None = None,
                 safety: SafetyLayer | None = None):
        self.discovery = discovery or WebMCPDiscovery()
        self.ownership = ownership or OwnershipGraph()
        self.leases = leases or LeaseManager(self.ownership)
        self.safety = safety or SafetyLayer()
        self.policy_engine = PolicyEngine(self.ownership, self.safety,
                                            leases=self.leases)

    def discover_and_register(self, origin: str) -> WebMCPSite | None:
        """Discover a site's WebMCP tools and register them as capabilities."""
        site = self.discovery.discover(origin)
        return site

    def execute(self, origin: str, tool_name: str, args: dict[str, Any],
                goal: str = "", user_consented: bool = False) -> ToolResult:
        """Execute a WebMCP tool through the full safety pipeline."""
        request_id = uuid.uuid4().hex[:8]

        # 1. Find capability
        cap = REGISTRY.get_by_name(origin, tool_name)
        if not cap:
            # Try to discover first
            self.discover_and_register(origin)
            cap = REGISTRY.get_by_name(origin, tool_name)
        if not cap:
            return ToolResult(request_id, False, error=f"tool '{tool_name}' not found on {origin}")

        # 2. Policy check
        decision = self.policy_engine.check(cap, goal, user_consented)
        if not decision.allowed:
            if decision.requires_consent:
                return ToolResult(request_id, False,
                                  error=f"REQUIRES_CONSENT: {decision.reason}")
            if decision.requires_ownership:
                return ToolResult(request_id, False,
                                  error=f"OWNERSHIP_CONFLICT: {decision.reason}")
            return ToolResult(request_id, False, error=f"POLICY_DENIED: {decision.reason}")

        # 3. Check capability-level policy
        if not CAPABILITY_POLICY.is_allowed(cap):
            return ToolResult(request_id, False,
                              error=f"capability source '{cap.source}' not allowed")

        # 4. Execute the tool (in production: Chrome WebMCP API)
        # For prototype: simulate execution
        result = self._execute_tool(origin, tool_name, args)

        # 5. Record capability outcome
        cap.record_result(result.ok)

        # 6. Verify result
        verified = self._verify_result(cap, result)
        if not verified:
            result = ToolResult(request_id, False, error="verification failed")

        return result

    def _execute_tool(self, origin: str, tool_name: str, args: dict) -> ToolResult:
        """Execute the WebMCP tool.

        LEGACY/PARTIAL: answers from the built-in fixture table; it never
        touches a page. Real execution goes through WebMCPGateway.
        """
        request_id = uuid.uuid4().hex[:8]

        # Fixture table (see harness/webmcp/fallback.py): simulated tool
        # execution for the legacy endpoint and benchmarks only.
        if tool_name in LEGACY_FALLBACK_TABLE:
            return ToolResult(request_id, True,
                              result=dict(LEGACY_FALLBACK_TABLE[tool_name]))
        return ToolResult(request_id, False, error=f"unknown tool: {tool_name}")

    def _verify_result(self, cap: Capability, result: ToolResult) -> bool:
        """Verify tool result meets expectations."""
        if not result.ok:
            return False
        # For read-only tools, verify we got data
        if cap.annotations.readOnlyHint and cap.source == "webmcp":
            return result.result is not None
        return True

    def list_capabilities(self, origin: str) -> list[Capability]:
        return REGISTRY.get_for_origin(origin)

    def find_capabilities(self, goal: str, origin: str | None = None) -> list[Capability]:
        return REGISTRY.find_capabilities(goal, origin)

    def choose_best_capability(self, goal: str, origin: str | None = None) -> Capability | None:
        return REGISTRY.choose_best(goal, origin)


# Global adapter instance
ADAPTER = WebMCPAdapter()
