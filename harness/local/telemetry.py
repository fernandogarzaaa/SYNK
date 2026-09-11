"""Telemetry: Tracking Beta.2 local vs cloud efficiency metrics.
"""
from __future__ import annotations
from dataclasses import dataclass, field
import time
from typing import Dict, List

@dataclass
class DecisionMetric:
    type: str # 'deterministic', 'local_slm', 'cloud_llm', 'cache'
    latency_ms: float
    tokens_saved: int = 0
    bytes_saved: int = 0
    cost_saved: float = 0.0
    confidence: float = 1.0
    was_escalated: bool = False

class LocalTelemetry:
    def __init__(self):
        self.decisions: List[DecisionMetric] = []
        self.total_cost_saved = 0.0
        self.total_tokens_saved = 0

    def record(self, metric: DecisionMetric):
        self.decisions.append(metric)
        self.total_cost_saved += metric.cost_saved
        self.total_tokens_saved += metric.tokens_saved

    def get_summary(self) -> Dict[str, Any]:
        if not self.decisions:
            return {}
        
        total = len(self.decisions)
        cloud_count = sum(1 for d in self.decisions if d.type == 'cloud_llm')
        local_count = sum(1 for d in self.decisions if d.type in ('local_slm', 'deterministic', 'cache'))
        
        return {
            "total_decisions": total,
            "local_decision_rate": local_count / total,
            "cloud_escalation_rate": cloud_count / total,
            "avg_local_latency": sum(d.latency_ms for d in self.decisions if d.type != 'cloud_llm') / max(1, local_count),
            "avg_cloud_latency": sum(d.latency_ms for d in self.decisions if d.type == 'cloud_llm') / max(1, cloud_count),
            "total_tokens_saved": self.total_tokens_saved,
            "total_cost_saved": self.total_cost_saved,
            "fallback_rate": sum(1 for d in self.decisions if d.was_escalated) / total
        }
