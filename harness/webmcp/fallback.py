"""Legacy local dispatch table: PARTIAL fallback ONLY (Stage E).

This table is the old ``WebMCPAdapter._execute_tool`` mock catalog. It is
NOT a transport and NOT a model context: nothing in it ever touched a
page. It survives for exactly two honest uses:

1. The legacy ``/webmcp/execute`` endpoint and the benchmark harness,
   which are explicitly labeled PARTIAL wherever they surface.
2. The opt-in ``--webmcp-fallback`` server flag: when a page exposes NO
   model context at all, the gateway may answer from this table instead
   of failing -- but every such use is recorded with the PARTIAL caveat
   below, and fixture results can NEVER verify a claim (the verifier
   refuses ``partial_fallback`` evidence outright).

When the page DOES advertise tools, the fallback is never consulted: a
tool the page did not advertise fails closed with WEBMCP_TOOL_NOT_ADVERTISED.
"""
from __future__ import annotations

# Fixture results keyed by tool name. Shape mirrors ToolResult.result.
LEGACY_FALLBACK_TABLE: dict[str, dict] = {
    "searchProducts": {"products": [{"id": "1", "name": "MacBook Pro",
                                     "price": 1999}]},
    "getProductDetails": {"product": {"id": "1", "name": "MacBook Pro",
                                     "price": 1999, "specs": "M3, 16GB"}},
    "addToCart": {"cart": {"items": [{"product_id": "1", "quantity": 1}],
                           "total": 1999}},
    "checkout": {"order_id": "ord_123", "status": "confirmed",
                 "total": 1999},
    "deleteAccount": {"deleted": True},
    "searchCustomers": {"customers": [{"id": "c1", "name": "John Doe",
                                       "email": "john@example.com"}]},
    "getCustomer": {"customer": {"id": "c1", "name": "John Doe",
                                 "email": "john@example.com"}},
    "updateCustomer": {"customer": {"id": "c1", "name": "John Doe",
                                    "email": "jane@example.com"}},
}

FALLBACK_CAVEAT = (
    "PARTIAL: this result came from SYNK's built-in fixture table, NOT "
    "from the page's model context. The page exposed no WebMCP tools. "
    "Treat the data as illustrative; it proves nothing about the live page, "
    "and it can never verify a claim.")
