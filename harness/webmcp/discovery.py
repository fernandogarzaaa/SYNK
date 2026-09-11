"""WebMCP discovery: find and parse site-exposed tools via Chrome WebMCP protocol.

In practice, this would use Chrome's CDP or WebMCP discovery endpoint.
For the prototype, we provide a mock discovery that simulates what a real
WebMCP-enabled site would expose.
"""
from __future__ import annotations

import json
from typing import Any

from .schema import WebMCPTool, WebMCPSite
from .registry import REGISTRY, Capability


# Mock site catalog for development/demo
MOCK_SITES = {
    "shop.example.com": WebMCPSite(
        origin="shop.example.com",
        tools=[
            WebMCPTool.from_dict({
                "name": "searchProducts",
                "description": "Search products by query and filters",
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "query": {"type": "string"},
                        "category": {"type": "string"},
                        "max_price": {"type": "number"},
                    },
                    "required": ["query"],
                },
                "annotations": {"readOnlyHint": True},
            }),
            WebMCPTool.from_dict({
                "name": "getProductDetails",
                "description": "Get detailed product info by ID",
                "input_schema": {
                    "type": "object",
                    "properties": {"product_id": {"type": "string"}},
                    "required": ["product_id"],
                },
                "annotations": {"readOnlyHint": True},
            }),
            WebMCPTool.from_dict({
                "name": "addToCart",
                "description": "Add a product to the shopping cart",
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "product_id": {"type": "string"},
                        "quantity": {"type": "integer", "minimum": 1},
                    },
                    "required": ["product_id"],
                },
                "annotations": {"readOnlyHint": False, "destructiveHint": False},
            }),
            WebMCPTool.from_dict({
                "name": "checkout",
                "description": "Complete purchase with payment and shipping",
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "payment_method_id": {"type": "string"},
                        "shipping_address_id": {"type": "string"},
                    },
                    "required": ["payment_method_id", "shipping_address_id"],
                },
                "annotations": {"readOnlyHint": False, "destructiveHint": True},
            }),
            WebMCPTool.from_dict({
                "name": "deleteAccount",
                "description": "Permanently delete user account",
                "input_schema": {"type": "object", "properties": {}},
                "annotations": {"readOnlyHint": False, "destructiveHint": True},
            }),
        ],
        discovered_at=0.0,
    ),
    "crm.example.com": WebMCPSite(
        origin="crm.example.com",
        tools=[
            WebMCPTool.from_dict({
                "name": "searchCustomers",
                "description": "Search customers by name/email",
                "input_schema": {
                    "type": "object",
                    "properties": {"query": {"type": "string"}},
                    "required": ["query"],
                },
                "annotations": {"readOnlyHint": True},
            }),
            WebMCPTool.from_dict({
                "name": "getCustomer",
                "description": "Get full customer record",
                "input_schema": {
                    "type": "object",
                    "properties": {"customer_id": {"type": "string"}},
                    "required": ["customer_id"],
                },
                "annotations": {"readOnlyHint": True},
            }),
            WebMCPTool.from_dict({
                "name": "updateCustomer",
                "description": "Update customer fields",
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "customer_id": {"type": "string"},
                        "fields": {"type": "object"},
                    },
                    "required": ["customer_id"],
                },
                "annotations": {"readOnlyHint": False},
            }),
        ],
        discovered_at=0.0,
    ),
}


class WebMCPDiscovery:
    """Discovers WebMCP tools from sites. In production, uses CDP/WebMCP API."""

    def __init__(self, mock_mode: bool = True):
        self.mock_mode = mock_mode
        self.discovered: dict[str, WebMCPSite] = {}

    def discover(self, origin: str) -> WebMCPSite | None:
        """Discover tools for an origin. Returns cached if available."""
        if origin in self.discovered:
            return self.discovered[origin]
        if self.mock_mode:
            site = MOCK_SITES.get(origin)
            if site:
                self._register_site(site)
                return site
        # In production: fetch from chrome.webmcp.discover(origin) or CDP
        return None

    def discover_from_url(self, url: str) -> WebMCPSite | None:
        try:
            origin = url.split("//", 1)[1].split("/", 1)[0].lower()
        except IndexError:
            return None
        return self.discover(origin)

    def _register_site(self, site: WebMCPSite) -> None:
        self.discovered[site.origin] = site
        for tool in site.tools:
            cap = Capability.from_webmcp(site.origin, tool)
            REGISTRY.register(cap)

    def get_all_sites(self) -> list[WebMCPSite]:
        return list(self.discovered.values())


def load_mock_sites() -> None:
    """Convenience: load all mock sites into registry."""
    disc = WebMCPDiscovery(mock_mode=True)
    for site in MOCK_SITES.values():
        disc._register_site(site)
