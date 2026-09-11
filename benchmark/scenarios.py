"""Benchmark scenarios: realistic co-working tasks for evaluation."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any
import sys
sys.path.insert(0, 'E:/workspace/ai-cowork-browser')


@dataclass
class Scenario:
    name: str
    description: str
    goal: str
    # Initial state
    initial_url: str
    human_actions: list[dict[str, Any]]  # actions human will perform
    agent_goal: str                      # what agent should achieve
    # Expected outcomes
    expected_agent_actions: int = 0
    expected_conflicts: int = 0
    tags: list[str] = None

    def __post_init__(self):
        if self.tags is None:
            self.tags = []


# Realistic co-working scenarios
SCENARIOS = [
    Scenario(
        name="research_and_extract",
        description="Human researches products; agent extracts prices/specs in background",
        goal="Find and compare laptop prices",
        initial_url="https://shop.example.com",
        human_actions=[
            {"type": "navigate", "url": "https://shop.example.com/laptops"},
            {"type": "click", "target": "product_macbook_pro"},
            {"type": "scroll", "direction": "down"},
            {"type": "click", "target": "product_thinkpad"},
        ],
        agent_goal="Extract name, price, specs for all visible laptop products",
        expected_agent_actions=3,
        expected_conflicts=0,
        tags=["multi-tab", "extraction", "read-only"],
    ),
    Scenario(
        name="form_completion",
        description="Human edits a few fields; agent fills repetitive fields simultaneously",
        goal="Complete checkout form",
        initial_url="https://shop.example.com/checkout",
        human_actions=[
            {"type": "type", "target": "email", "text": "user@example.com"},
            {"type": "click", "target": "shipping_address_dropdown"},
        ],
        agent_goal="Fill remaining shipping/billing fields from memory",
        expected_agent_actions=4,
        expected_conflicts=1,  # human clicks dropdown while agent tries to fill
        tags=["form", "concurrent", "mutating"],
    ),
    Scenario(
        name="multi_tab_workflow",
        description="Human works in Tab A; agent works in Tabs B/C/D",
        goal="Gather competitor pricing while human reviews primary product",
        initial_url="https://shop.example.com/product/main",
        human_actions=[
            {"type": "click", "target": "reviews_tab"},
            {"type": "scroll", "direction": "down"},
        ],
        agent_goal="Open competitor tabs, extract prices, summarize in sidebar",
        expected_agent_actions=6,
        expected_conflicts=0,
        tags=["multi-tab", "parallel", "background"],
    ),
    Scenario(
        name="crm_customer_lookup",
        description="Human searches customer; agent opens record and copies details",
        goal="Look up customer and extract account info",
        initial_url="https://crm.example.com",
        human_actions=[
            {"type": "type", "target": "search_box", "text": "Acme Corp"},
            {"type": "click", "target": "search_button"},
        ],
        agent_goal="When result appears, open record and extract phone, email, account number",
        expected_agent_actions=3,
        expected_conflicts=1,  # human clicks result while agent also tries
        tags=["crm", "workflow", "repetitive"],
    ),
    Scenario(
        name="travel_booking",
        description="Human selects dates/preferences; agent fills traveler info and submits",
        goal="Book flight and hotel",
        initial_url="https://travel.example.com",
        human_actions=[
            {"type": "click", "target": "date_picker"},
            {"type": "click", "target": "departure_date"},
            {"type": "click", "target": "return_date"},
            {"type": "click", "target": "search_flights"},
        ],
        agent_goal="Fill traveler details, loyalty numbers, payment; submit booking",
        expected_agent_actions=5,
        expected_conflicts=2,  # human interacts with date picker, agent fills fields
        tags=["travel", "booking", "sensitive", "multi-step"],
    ),
]


def get_scenario(name: str) -> Scenario | None:
    for s in SCENARIOS:
        if s.name == name:
            return s
    return None


def list_scenarios() -> list[Scenario]:
    return list(SCENARIOS)
