"""WebMCP Verifier: validates tool results against expected outcomes.

Ensures that WebMCP tool execution produces valid, expected results before
returning to the agent. This is part of the transactional execution pipeline.
"""
from __future__ import annotations

from typing import Any

from .schema import ToolResult, WebMCPTool


class VerificationError(Exception):
    pass


class WebMCPVerifier:
    """Verifies WebMCP tool results."""

    def __init__(self):
        self.rules: dict[str, list[callable]] = {}

    def register_rule(self, tool_name: str, rule: callable) -> None:
        """Register a verification rule for a tool.
        Rule signature: rule(result: ToolResult, args: dict) -> bool
        """
        self.rules.setdefault(tool_name, []).append(rule)

    def verify(self, tool: WebMCPTool, result: ToolResult, args: dict) -> tuple[bool, str]:
        """Verify a tool result. Returns (ok, error_message)."""
        if not result.ok:
            return False, result.error or "tool execution failed"

        rules = self.rules.get(tool.name, [])
        for rule in rules:
            try:
                if not rule(result, args):
                    return False, f"verification rule failed for {tool.name}"
            except Exception as e:
                return False, f"verification error: {e}"

        # Default checks
        if tool.is_read_only and result.result is None:
            return False, "read-only tool returned no data"

        return True, ""

    def verify_schema(self, tool: WebMCPTool, result: ToolResult) -> bool:
        """Basic schema validation of result against tool's output expectations."""
        # In a full implementation, this would validate against an output schema
        # For now, just check that result has expected structure
        if not result.ok:
            return False
        return True


# Default verifier instance
VERIFIER = WebMCPVerifier()

# Register some default rules
def _verify_search_products(result: ToolResult, args: dict) -> bool:
    if not isinstance(result.result, dict):
        return False
    return "products" in result.result

def _verify_get_product(result: ToolResult, args: dict) -> bool:
    if not isinstance(result.result, dict):
        return False
    return "product" in result.result

def _verify_add_to_cart(result: ToolResult, args: dict) -> bool:
    if not isinstance(result.result, dict):
        return False
    return "cart" in result.result

def _verify_checkout(result: ToolResult, args: dict) -> bool:
    if not isinstance(result.result, dict):
        return False
    return "order_id" in result.result

VERIFIER.register_rule("searchProducts", _verify_search_products)
VERIFIER.register_rule("getProductDetails", _verify_get_product)
VERIFIER.register_rule("addToCart", _verify_add_to_cart)
VERIFIER.register_rule("checkout", _verify_checkout)
