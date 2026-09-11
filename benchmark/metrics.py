"""Benchmark metrics and utilities for Beta.1 evaluation."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any
import time
import sys
sys.path.insert(0, 'E:/workspace/ai-cowork-browser')


@dataclass
class TaskMetrics:
    """Detailed metrics for a single task run."""
    task_name: str
    agent_type: str  # "naive" | "dom" | "beta" | "webmcp" | "human"

    # Time
    ttfa: float = 0.0          # time to first action (s)
    ttla: float = 0.0          # time to last action (s)
    total_time: float = 0.0    # total task time (s)

    # LLM usage
    llm_calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0

    # Browser interactions
    browser_actions: int = 0
    screenshots: int = 0
    context_bytes: int = 0

    # System
    cpu_time: float = 0.0
    memory_mb: float = 0.0

    # Outcomes
    success: bool = False
    failure_reason: str = ""
    recovery_count: int = 0

    # Co-working specific
    human_interruptions: int = 0
    conflicts: int = 0
    auto_resolved: int = 0
    agent_interference_events: int = 0

    # Beta.2: Local Intelligence Runtime
    local_decision_rate: float = 0.0
    cloud_escalation_rate: float = 0.0
    local_decision_latency: float = 0.0
    cloud_decision_latency: float = 0.0
    tokens_saved: int = 0
    context_bytes_saved: int = 0
    cost_saved: float = 0.0
    decision_accuracy: float = 0.0
    fallback_rate: float = 0.0

    # Custom
    extra: dict[str, Any] = field(default_factory=dict)

    @property
    def total_tokens(self) -> int:
        return self.input_tokens + self.output_tokens

    @property
    def interference_rate(self) -> float:
        """Agent-caused human disruptions / total human interactions."""
        if self.human_interruptions == 0:
            return 0.0
        return self.agent_interference_events / self.human_interruptions

    @property
    def agent_overlap_rate(self) -> float:
        """Conflicting agent/human intents / total concurrent actions."""
        total = self.browser_actions + self.human_interruptions
        if total == 0:
            return 0.0
        return self.conflicts / total

    @property
    def recovered_conflict_rate(self) -> float:
        """Conflicts automatically resolved / total conflicts."""
        if self.conflicts == 0:
            return 1.0
        return self.auto_resolved / self.conflicts

    @property
    def agent_tax(self) -> float:
        """Additional overhead from AI involvement (lower is better).
        Normalized: 1.0 = same as human-only, >1.0 = AI adds overhead.
        """
        if self.total_time == 0:
            return 0.0
        # This would be calibrated against human-only baseline
        return self.total_tokens / max(1, self.browser_actions) + self.total_time * 0.1


class BenchmarkTimer:
    """Context manager for timing."""

    def __init__(self):
        self.start = 0.0
        self.end = 0.0

    def __enter__(self):
        self.start = time.perf_counter()
        return self

    def __exit__(self, *args):
        self.end = time.perf_counter()

    @property
    def elapsed(self) -> float:
        return self.end - self.start


def compute_aes(metrics: TaskMetrics) -> float:
    """Agent Efficiency Score: success / (tokens + latency_w + actions_w)."""
    W_LAT, W_ACT = 0.5, 50.0
    denom = metrics.total_tokens + W_LAT * metrics.total_time + W_ACT * metrics.browser_actions
    return (1.0 if metrics.success else 0.0) / denom if denom > 0 else 0.0


def compute_interference_score(metrics: TaskMetrics) -> float:
    """Composite interference metric (lower is better)."""
    return (metrics.interference_rate * 0.4 +
            metrics.agent_overlap_rate * 0.3 +
            (1.0 - metrics.recovered_conflict_rate) * 0.3)


def format_metrics(m: TaskMetrics) -> str:
    return (
        f"{m.task_name:25s} | {m.agent_type:8s} | "
        f"success={m.success} | time={m.total_time:.2f}s | "
        f"tokens={m.total_tokens:5d} | actions={m.browser_actions:3d} | "
        f"conflicts={m.conflicts} | interference={m.interference_rate:.2f} | "
        f"AES={compute_aes(m):.6f}"
    )
