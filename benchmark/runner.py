"""Benchmark runner: executes all agents against all scenarios and reports results."""

from __future__ import annotations

from typing import Any
import json
import time
import sys
sys.path.insert(0, 'E:/workspace/ai-cowork-browser')

from benchmark.agents import create_all_agents, BaseAgent
from benchmark.scenarios import list_scenarios, Scenario
from benchmark.metrics import TaskMetrics, compute_aes, compute_interference_score, format_metrics


def run_benchmark(agents: list[BaseAgent], scenarios: list[Scenario] = None,
                  verbose: bool = True) -> dict:
    """Run all agents against all scenarios."""
    if scenarios is None:
        scenarios = list_scenarios()

    all_results = []
    summary = {}

    for scenario in scenarios:
        if verbose:
            print(f"\n{'='*60}")
            print(f"Scenario: {scenario.name} - {scenario.description}")
            print(f"{'='*60}")

        human_actions = scenario.human_actions
        scenario_results = {}

        for agent in agents:
            if verbose:
                print(f"  Running {agent.name}...")

            try:
                result = agent.run(scenario, human_actions)

                metrics = TaskMetrics(
                    task_name=scenario.name,
                    agent_type=agent.name,
                    ttfa=result.extra.get("ttfa", 0),
                    ttla=result.extra.get("ttla", 0),
                    total_time=result.extra.get("ttla", 0),
                    llm_calls=result.llm_calls,
                    input_tokens=result.tokens_used,
                    output_tokens=0,
                    browser_actions=len(result.actions),
                    screenshots=result.extra.get("screenshots", 0),
                    context_bytes=result.tokens_used * 4,
                    cpu_time=result.extra.get("ttla", 0),
                    memory_mb=50.0,
                    success=result.success,
                    failure_reason="" if result.success else "no actions",
                    recovery_count=result.extra.get("auto_resolved", 0),
                    human_interruptions=result.human_interruptions,
                    conflicts=result.conflicts,
                    auto_resolved=result.extra.get("auto_resolved", 0),
                    agent_interference_events=result.conflicts,
                    # Beta.2 local intelligence metrics
                    local_decision_rate=result.extra.get("local_decision_rate", 0.0),
                    cloud_escalation_rate=result.extra.get("cloud_escalation_rate", 0.0),
                    local_decision_latency=result.extra.get("local_decision_latency", 0.0),
                    cloud_decision_latency=result.extra.get("cloud_decision_latency", 0.0),
                    tokens_saved=result.extra.get("tokens_saved", 0),
                    context_bytes_saved=result.extra.get("context_bytes_saved", 0),
                    cost_saved=result.extra.get("cost_saved", 0.0),
                    decision_accuracy=result.extra.get("decision_accuracy", 0.0),
                    fallback_rate=result.extra.get("fallback_rate", 0.0),
                    extra=result.extra or {},
                )

                all_results.append(metrics)
                scenario_results[agent.name] = metrics

                if verbose:
                    print(f"    {format_metrics(metrics)}")

            except Exception as e:
                if verbose:
                    print(f"    ERROR: {e}")
                metrics = TaskMetrics(
                    task_name=scenario.name,
                    agent_type=agent.name,
                    success=False,
                    failure_reason=str(e),
                )
                all_results.append(metrics)
                scenario_results[agent.name] = metrics

        summary[scenario.name] = scenario_results

    # Aggregate summary
    agents_list = [a.name for a in agents]
    overall = {}
    for agent_name in agents_list:
        agent_metrics = [m for m in all_results if m.agent_type == agent_name]
        if agent_metrics:
            overall[agent_name] = {
                "tasks": len(agent_metrics),
                "success_rate": sum(1 for m in agent_metrics if m.success) / len(agent_metrics),
                "mean_aes": sum(compute_aes(m) for m in agent_metrics) / len(agent_metrics),
                "mean_interference": sum(compute_interference_score(m) for m in agent_metrics) / len(agent_metrics),
                "total_tokens": sum(m.total_tokens for m in agent_metrics),
                "total_time": sum(m.total_time for m in agent_metrics),
                "total_conflicts": sum(m.conflicts for m in agent_metrics),
            }

    return {
        "per_task": [m.__dict__ for m in all_results],
        "by_scenario": {
            s: {a: m.__dict__ for a, m in res.items()}
            for s, res in summary.items()
        },
        "overall": overall,
        "timestamp": time.time(),
    }


def print_summary(results: dict):
    """Print formatted benchmark summary."""
    print("\n" + "="*80)
    print("BENCHMARK SUMMARY")
    print("="*80)

    print("\n--- Overall Agent Ranking ---")
    overall = results.get("overall", {})
    for agent, stats in sorted(overall.items(),
                               key=lambda x: x[1].get("mean_aes", 0), reverse=True):
        print(f"  {agent:12s} | success={stats['success_rate']:.0%} | "
              f"AES={stats['mean_aes']:.6f} | interference={stats['mean_interference']:.3f} | "
              f"tokens={stats['total_tokens']} | conflicts={stats['total_conflicts']}")

    print("\n--- Per-Scenario Comparison ---")
    by_scenario = results.get("by_scenario", {})
    for scenario, agent_results in by_scenario.items():
        print(f"\n  {scenario}:")
        for agent, metrics in sorted(agent_results.items(),
                                     key=lambda x: compute_aes(TaskMetrics(**x[1])) if x[1].get("success") else -1,
                                     reverse=True):
            m = TaskMetrics(**metrics)
            aes = compute_aes(m)
            print(f"    {agent:12s} | success={m.success} | AES={aes:.6f} | "
                  f"time={m.total_time:.2f}s | tokens={m.total_tokens} | "
                  f"conflicts={m.conflicts} | interference={compute_interference_score(m):.3f}")

    # Key thesis test: D < C < B < A for meaningful workloads
    print("\n--- Thesis Test: D (WebMCP) < C (Beta) < B (DOM) < A (Naive) ---")
    for scenario in list_scenarios():
        sname = scenario.name
        if sname in by_scenario:
            res = by_scenario[sname]
            if all(k in res for k in ["naive", "dom", "beta", "webmcp"]):
                a_aes = compute_aes(TaskMetrics(**res["naive"]))
                b_aes = compute_aes(TaskMetrics(**res["dom"]))
                c_aes = compute_aes(TaskMetrics(**res["beta"]))
                d_aes = compute_aes(TaskMetrics(**res["webmcp"]))
                # Higher AES is better
                order = "D>C>B>A" if d_aes > c_aes > b_aes > a_aes else "VIOLATED"
                print(f"  {sname}: A={a_aes:.6f} B={b_aes:.6f} C={c_aes:.6f} D={d_aes:.6f} -> {order}")


def save_results(results: dict, path: str = "benchmark/results.json"):
    """Save benchmark results to JSON."""
    import os
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        json.dump(results, f, indent=2, default=str)


if __name__ == "__main__":
    agents = create_all_agents()
    results = run_benchmark(agents)
    print_summary(results)
    save_results(results)
