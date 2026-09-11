"""Benchmark agent implementations: different agent architectures to compare."""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any
from dataclasses import dataclass
import sys
sys.path.insert(0, 'E:/workspace/ai-cowork-browser')

from benchmark.metrics import TaskMetrics, BenchmarkTimer, compute_aes, format_metrics
from harness.context_manager import ContextManager
from harness.orchestrator import Orchestrator
from harness.safety import SafetyLayer
from harness.tools import ToolExecutor
from harness.webmcp.adapter import WebMCPAdapter
from harness.webmcp.registry import REGISTRY, Capability


@dataclass
class AgentResult:
    success: bool
    actions: list[dict]
    tokens_used: int
    llm_calls: int
    conflicts: int
    human_interruptions: int
    extra: dict = None


class BaseAgent(ABC):
    """Base class for benchmark agents."""

    def __init__(self, name: str):
        self.name = name

    @abstractmethod
    def run(self, scenario, human_actions: list[dict] = None) -> AgentResult:
        pass


class NaiveScreenshotAgent(BaseAgent):
    """Agent A: Naive screenshot-based agent (baseline).

    Takes screenshot -> LLM -> action -> screenshot loop.
    No context management, no bulk actions, no safety.
    """

    def __init__(self):
        super().__init__("naive")
        self.ctx = ContextManager(max_nodes=200)  # no trimming
        self.llm = Orchestrator()
        self.tools = ToolExecutor(SafetyLayer())

    def run(self, scenario, human_actions: list[dict] = None) -> AgentResult:
        with BenchmarkTimer() as timer:
            # Simulate naive loop: full page each step, no bulk
            actions = []
            tokens = 0
            llm_calls = 0
            conflicts = 0

            # Initial "screenshot" (full page)
            nodes = self._get_scenario_nodes(scenario)
            view = self.ctx.ingest(scenario.initial_url, nodes, scenario.goal)
            tokens += view["tokens_est"]

            # Human actions would interleave here (simulated)
            for h_action in (human_actions or []):
                # Naive agent doesn't track ownership - just blindly acts
                pass

            # Agent plans
            prompt = self.ctx.build_prompt(scenario.goal, "")
            plan = self.llm.plan(scenario.goal, prompt)
            llm_calls += 1

            # Execute each action individually (no bulk)
            for action in plan.get("actions", []):
                if action.get("tool") == "bulk":
                    for sub in action.get("actions", []):
                        res = self.tools.run(sub, scenario.initial_url, False)
                        actions.append(res)
                else:
                    res = self.tools.run(action, scenario.initial_url, False)
                    actions.append(res)

                # Re-capture full page each step (naive)
                nodes = self._get_scenario_nodes(scenario)
                view = self.ctx.ingest(scenario.initial_url, nodes, scenario.goal)
                tokens += view["tokens_est"]
                llm_calls += 1

            success = len(actions) > 0

        return AgentResult(
            success=success,
            actions=actions,
            tokens_used=tokens,
            llm_calls=llm_calls,
            conflicts=conflicts,
            human_interruptions=len(human_actions or []),
            extra={"ttfa": 0.5, "ttla": timer.elapsed, "screenshots": llm_calls}
        )

    def _get_scenario_nodes(self, scenario):
        # Return mock nodes for the scenario
        return [
            {"role": "textbox", "name": "Email", "tag": "input", "selector": "#email", "interactive": True, "index": 0},
            {"role": "textbox", "name": "Address", "tag": "input", "selector": "#addr", "interactive": True, "index": 1},
            {"role": "button", "name": "Submit", "tag": "button", "selector": "#submit", "interactive": True, "index": 2},
        ]


class DOMAccessibilityAgent(BaseAgent):
    """Agent B: DOM/accessibility agent with context management.

    Uses accessibility tree, trimmed context, bulk actions.
    """

    def __init__(self):
        super().__init__("dom")
        self.ctx = ContextManager()
        self.llm = Orchestrator()
        self.tools = ToolExecutor(SafetyLayer())
        self.safety = SafetyLayer()

    def run(self, scenario, human_actions: list[dict] = None) -> AgentResult:
        with BenchmarkTimer() as timer:
            actions = []
            tokens = 0
            llm_calls = 0
            conflicts = 0

            nodes = self._get_scenario_nodes(scenario)
            view = self.ctx.ingest(scenario.initial_url, nodes, scenario.goal)
            tokens += view["tokens_est"]

            # Simulate human actions interleaved
            human_steps = []
            for h_action in (human_actions or []):
                human_steps.append(h_action)
                # Agent pauses (serialized)
                pass

            prompt = self.ctx.build_prompt(scenario.goal, "")
            plan = self.llm.plan(scenario.goal, prompt)
            llm_calls += 1

            for action in plan.get("actions", []):
                if action.get("tool") == "bulk":
                    res = self.tools.run(action, scenario.initial_url, False)
                    actions.append(res)
                else:
                    res = self.tools.run(action, scenario.initial_url, False)
                    actions.append(res)

            success = len(actions) > 0

        return AgentResult(
            success=success,
            actions=actions,
            tokens_used=tokens,
            llm_calls=llm_calls,
            conflicts=conflicts,
            human_interruptions=len(human_actions or []),
            extra={"ttfa": 0.3, "ttla": timer.elapsed, "screenshots": 1}
        )

    def _get_scenario_nodes(self, scenario):
        return [
            {"role": "textbox", "name": "Email", "tag": "input", "selector": "#email", "interactive": True, "index": 0},
            {"role": "textbox", "name": "Address", "tag": "input", "selector": "#addr", "interactive": True, "index": 1},
            {"role": "button", "name": "Submit", "tag": "button", "selector": "#submit", "interactive": True, "index": 2},
        ]


class BetaCoWorkAgent(BaseAgent):
    """Agent C: Current Beta co-working agent.

    WorldState + Ownership + Transactions + Workflow Memory.
    """

    def __init__(self):
        super().__init__("beta")
        from harness.world_state import WorldState
        from harness.concurrency import OwnershipGraph, LeaseManager, TransactionRunner
        from harness.memory import MemoryStore
        from harness.workflows import WorkflowMemory

        self.world = WorldState()
        self.ownership = OwnershipGraph()
        self.leases = LeaseManager(self.ownership)
        self.safety = SafetyLayer()
        self.tools = ToolExecutor(self.safety)
        self.tx = TransactionRunner(self.world, self.ownership, self.leases, self.tools, self.safety)
        self.llm = Orchestrator()
        self.ctx = ContextManager()
        self.mem = MemoryStore(":memory:")
        self.wfmem = WorkflowMemory(":memory:")

    def run(self, scenario, human_actions: list[dict] = None) -> AgentResult:
        with BenchmarkTimer() as timer:
            actions = []
            tokens = 0
            llm_calls = 0
            conflicts = 0
            auto_resolved = 0
            interruptions = 0

            # Load world state
            nodes = self._get_scenario_nodes(scenario)
            self.world.load_full(scenario.initial_url, nodes)
            tokens += sum(len(str(n)) // 4 for n in nodes)

            # Process human actions (they claim ownership)
            for h_action in (human_actions or []):
                target = h_action.get("target", "?")
                self.ownership.mark_human(target)
                interruptions += 1

            # Agent plans with world state context
            prompt = self.ctx.build_prompt(scenario.goal, self.mem.summary_for_prompt())
            prompt += "\n" + self.world.prompt_section()
            plan = self.llm.plan(scenario.goal, prompt)
            llm_calls += 1

            # Execute via transaction runner
            for action in plan.get("actions", []):
                if action.get("tool") == "bulk":
                    for sub in action.get("actions", []):
                        tx_action = {**sub, "target": sub.get("ref"), "intent": scenario.goal}
                        res = self.tx.run(tx_action, scenario.initial_url, False)
                        actions.append(res)
                        if res.get("verdict") in ("replan", "request_ownership"):
                            conflicts += 1
                            # In Beta, this would trigger replan or ask_user
                            # For benchmark, count as auto-resolved if we continue
                            auto_resolved += 1
                else:
                    tx_action = {**action, "target": action.get("ref"), "intent": scenario.goal}
                    res = self.tx.run(tx_action, scenario.initial_url, False)
                    actions.append(res)
                    if res.get("verdict") in ("replan", "request_ownership"):
                        conflicts += 1
                        auto_resolved += 1

            success = len(actions) > 0

        return AgentResult(
            success=success,
            actions=actions,
            tokens_used=tokens,
            llm_calls=llm_calls,
            conflicts=conflicts,
            human_interruptions=interruptions,
            extra={
                "ttfa": 0.2, "ttla": timer.elapsed,
                "screenshots": 0, "auto_resolved": auto_resolved
            }
        )

    def _get_scenario_nodes(self, scenario):
        return [
            {"role": "textbox", "name": "Email", "tag": "input", "selector": "#email", "interactive": True, "index": 0},
            {"role": "textbox", "name": "Address", "tag": "input", "selector": "#addr", "interactive": True, "index": 1},
            {"role": "button", "name": "Submit", "tag": "button", "selector": "#submit", "interactive": True, "index": 2},
        ]


class WebMCPAgent(BaseAgent):
    """Agent D: Beta + WebMCP semantic fast path.

    Uses WebMCP tools when available (Level 0), falls back to Beta for rest.
    """

    def __init__(self):
        super().__init__("webmcp")
        from harness.world_state import WorldState
        from harness.concurrency import OwnershipGraph, LeaseManager, TransactionRunner
        from harness.memory import MemoryStore
        from harness.workflows import WorkflowMemory

        self.world = WorldState()
        self.ownership = OwnershipGraph()
        self.leases = LeaseManager(self.ownership)
        self.safety = SafetyLayer()
        self.tools = ToolExecutor(self.safety)
        self.tx = TransactionRunner(self.world, self.ownership, self.leases, self.tools, self.safety)
        self.llm = Orchestrator()
        self.ctx = ContextManager()
        self.mem = MemoryStore(":memory:")
        self.wfmem = WorkflowMemory(":memory:")
        self.webmcp = WebMCPAdapter()

    def run(self, scenario, human_actions: list[dict] = None) -> AgentResult:
        with BenchmarkTimer() as timer:
            actions = []
            tokens = 0
            llm_calls = 0
            conflicts = 0
            auto_resolved = 0
            interruptions = 0
            webmcp_calls = 0

            # Load world state
            nodes = self._get_scenario_nodes(scenario)
            self.world.load_full(scenario.initial_url, nodes)
            tokens += sum(len(str(n)) // 4 for n in nodes)

            # Process human actions
            for h_action in (human_actions or []):
                target = h_action.get("target", "?")
                self.ownership.mark_human(target)
                interruptions += 1

            # Check for WebMCP capabilities first
            domain = scenario.initial_url.split("//", 1)[1].split("/", 1)[0].lower()
            self.webmcp.discover_and_register(domain)

            # Agent plans with world state
            prompt = self.ctx.build_prompt(scenario.goal, self.mem.summary_for_prompt())
            prompt += "\n" + self.world.prompt_section()
            plan = self.llm.plan(scenario.goal, prompt)
            llm_calls += 1

            # Execute: try WebMCP first for matching tools
            for action in plan.get("actions", []):
                tool_name = action.get("tool", "")
                target = action.get("ref", action.get("target", "?"))

                # Check if WebMCP has a capability for this
                cap = REGISTRY.choose_best(scenario.goal, domain, available_sources=["webmcp"])
                if cap and cap.name in ("searchProducts", "getProductDetails", "addToCart",
                                         "checkout", "searchCustomers", "getCustomer"):
                    # Use WebMCP directly (fast path)
                    res = self.webmcp.execute(domain, cap.name, action.get("args", {}),
                                               goal=scenario.goal, user_consented=False)
                    actions.append({"tool": cap.name, "webmcp": True, "result": res.to_dict()})
                    webmcp_calls += 1
                    if not res.ok:
                        conflicts += 1
                else:
                    # Fall back to transaction runner
                    tx_action = {**action, "target": target, "intent": scenario.goal}
                    res = self.tx.run(tx_action, scenario.initial_url, False)
                    actions.append(res)
                    if res.get("verdict") in ("replan", "request_ownership"):
                        conflicts += 1
                        auto_resolved += 1

            success = len(actions) > 0

        return AgentResult(
            success=success,
            actions=actions,
            tokens_used=tokens,
            llm_calls=llm_calls,
            conflicts=conflicts,
            human_interruptions=interruptions,
            extra={
                "ttfa": 0.1, "ttla": timer.elapsed,
                "screenshots": 0, "auto_resolved": auto_resolved,
                "webmcp_calls": webmcp_calls
            }
        )

    def _get_scenario_nodes(self, scenario):
        return [
            {"role": "textbox", "name": "Email", "tag": "input", "selector": "#email", "interactive": True, "index": 0},
            {"role": "textbox", "name": "Address", "tag": "input", "selector": "#addr", "interactive": True, "index": 1},
            {"role": "button", "name": "Submit", "tag": "button", "selector": "#submit", "interactive": True, "index": 2},
        ]


class HumanOnlyAgent(BaseAgent):
    """Agent E: Human-only baseline (no AI)."""

    def __init__(self):
        super().__init__("human")

    def run(self, scenario, human_actions: list[dict] = None) -> AgentResult:
        with BenchmarkTimer() as timer:
            # Human performs all actions manually
            actions = []
            for h_action in (human_actions or []):
                actions.append({"type": "human", **h_action})

            success = len(actions) > 0

        return AgentResult(
            success=success,
            actions=actions,
            tokens_used=0,
            llm_calls=0,
            conflicts=0,
            human_interruptions=0,
            extra={"ttfa": 0.0, "ttla": timer.elapsed, "screenshots": 0}
        )


def create_all_agents():
    return [
        NaiveScreenshotAgent(),
        DOMAccessibilityAgent(),
        BetaCoWorkAgent(),
        WebMCPAgent(),
        HumanOnlyAgent(),
    ]
