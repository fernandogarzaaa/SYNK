"""Benchmark harness: measure efficiency, not just completion (Beta §18).

Per-task metrics: success, time, LLM calls, input/output tokens, browser
actions, context bytes, human interruptions, agent-human conflicts, recovery
count, cost.

  Agent Efficiency Score: AES = task_success / (tokens + latency_w + actions_w)

Headline metric stays Human Productivity Gain (human alone vs conventional AI
browser vs this browser), but AES is what CI gates on.
"""
from __future__ import annotations

import time

W_LAT, W_ACT = 0.5, 50.0  # latency (s) and action weights for AES


def aes(success: float, tokens: int, latency_s: float, actions: int) -> float:
    denom = tokens + W_LAT * latency_s + W_ACT * actions
    return success / denom if denom > 0 else 0.0


def run_task(name: str, fn, **ctx) -> dict:
    """Run one benchmark task fn(ctx) -> dict(success, tokens, actions, ...)."""
    t0 = time.time()
    try:
        out = fn(**ctx) or {}
        success = float(out.get("success", 0.0))
    except Exception as e:
        out, success = {"error": str(e)}, 0.0
    dt = time.time() - t0
    m = {"task": name, "success": success,
         "latency_s": round(dt, 3),
         "llm_calls": out.get("llm_calls", 0),
         "tokens": out.get("tokens", 0),
         "browser_actions": out.get("actions", 0),
         "context_bytes": out.get("context_bytes", 0),
         "human_interruptions": out.get("interruptions", 0),
         "conflicts": out.get("conflicts", 0),
         "recoveries": out.get("recoveries", 0),
         "cost_usd": out.get("cost_usd", 0.0)}
    m["aes"] = aes(success, m["tokens"], m["latency_s"], m["browser_actions"])
    if "error" in out:
        m["error"] = out["error"]
    return m


def suite(tasks: list[tuple[str, callable]], **ctx) -> dict:
    results = [run_task(n, fn, **ctx) for n, fn in tasks]
    ok = [r for r in results if r["success"] >= 1.0]
    return {"results": results,
            "summary": {
                "tasks": len(results),
                "success_rate": round(len(ok) / len(results), 3) if results else 0,
                "mean_aes": round(sum(r["aes"] for r in results) / len(results), 6) if results else 0,
                "total_tokens": sum(r["tokens"] for r in results),
                "total_conflicts": sum(r["conflicts"] for r in results),
                "total_cost_usd": round(sum(r["cost_usd"] for r in results), 4)}}
