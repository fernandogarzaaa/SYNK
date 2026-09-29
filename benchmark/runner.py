"""SYNK 2.0 benchmark runner (Stage H / Phase 8).

Every number in benchmark/results.json is MEASURED by running this file
against the real harness. The pre-2.0 comparative-agent suite
(benchmark/agents.py, benchmark/scenarios.py) was REMOVED: it ran mock
page nodes through hard-coded per-agent timings (ttfa 0.5/0.3/0.2/0.1),
so its "rankings" were not measured and could never be trusted.

Data-source labels:
  MEASURED    - timed on this machine by this runner (wall clock)
  COMPUTED    - deterministic outcome of real harness logic (accuracy)
  TEST DOUBLE - a fake stands in for the browser; the harness path is real

What is measured, and the exact path each number comes from:

1. gateway_throughput (MEASURED): N single-action requests through
   ExecutionGateway.execute() -> TransactionEngine (strict mode) in
   extension mode (no browser attached). Covers validate -> lease ->
   policy -> dispatch -> ack -> observe -> verify per action. Actions end
   UNVERIFIED (no independent observation without a browser); the timing
   measures harness overhead, not browser interaction.

2. verification_accuracy (COMPUTED): known-good and known-bad
   claim/evidence pairs through the real Verifier. Each pair declares
   the expected outcome (VERIFIED / FAILED / CONFLICTING / UNVERIFIED);
   accuracy = correct / total. This measures the verifier's decision
   logic, not a browser.

3. lease_contention (MEASURED): T threads race to acquire one exclusive
   lease target through the real LeaseManager. Asserts exclusivity held
   (a shared counter never exceeds 1 inside the critical section) and
   reports acquire/refuse counts and per-attempt latency.

4. policy_latency (MEASURED): per-call latency of
   OriginPolicyRegistry.check_action (the full path: base decision +
   escalation review) over N calls.

5. scheduler_dispatch (COMPUTED + MEASURED): tasks with dependencies
   submitted to the real TaskScheduler and dispatched through run_next()
   with a recording exec_fn. Correctness = dispatch order respects
   dependencies, FIFO among ready tasks, tab-lease blocking honored.
   Also reports per-task dispatch latency.

NOT measured (no browser in this environment): end-to-end browser
interaction timings, real-page verification rates. REPORT.md labels
these UNMEASURED.

Run:  python benchmark/runner.py
"""
from __future__ import annotations

import json
import os
import statistics
import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from benchmark.metrics import mean, percentile
from harness.world_state import WorldState
from harness.context_manager import ContextManager
from harness.concurrency import OwnershipGraph, LeaseManager
from harness.safety import SafetyLayer
from harness.tools import ToolExecutor
from harness.transactions import TransactionEngine
from harness.gateway import ExecutionGateway
from harness.policy import OriginPolicyRegistry
from harness.task_scheduler import TaskScheduler, BLOCKED, COMPLETED
from harness.verification.verifier import Verifier
from harness.verification.claims import Claim, CLAIM_TYPE_STATE
from harness.verification.evidence import (
    Evidence, ELEMENT_STATE, DOM_CHANGE, URL_CHANGE, BROWSER_EVENT,
    BROWSER_ACK, SCREENSHOT, WEBMCP_RESULT, HUMAN_EVENT)
from harness.verification.results import (VERIFIED, FAILED, UNVERIFIED,
                                          CONFLICTING)


# -- shared harness ---------------------------------------------------------------

def _nodes():
    return [
        {"ref": 1, "role": "textbox", "name": "Email", "tag": "input",
         "selector": "#email", "interactive": True, "index": 0,
         "value": "", "disabled": False},
        {"ref": 2, "role": "button", "name": "Submit", "tag": "button",
         "selector": "#submit", "interactive": True, "index": 1,
         "disabled": False},
    ]


def make_engine():
    world = WorldState()
    ownership = OwnershipGraph()
    leases = LeaseManager(ownership)
    safety = SafetyLayer()
    ctx = ContextManager()
    verifier = Verifier(world)
    tools = ToolExecutor(safety)
    engine = TransactionEngine(world, ownership, leases, tools, safety,
                               ctx, verifier)
    # Stage F default-deny: the benchmark origin is explicitly trusted.
    engine.policy.register_origin(
        "bench.shop", allow=["read", "navigate", "interact", "webmcp"],
        description="benchmark origin")
    view = ctx.ingest("https://bench.shop/x", _nodes(), "fill form",
                      tab_id="t1")
    world.load_full("https://bench.shop/x", view["nodes"], "Bench", tab_id="t1")
    gateway = ExecutionGateway(engine)
    return gateway


# -- 1. gateway throughput ----------------------------------------------------------

def bench_gateway_throughput(n_actions: int = 200) -> dict:
    """MEASURED: actions/sec through the full gateway (extension mode)."""
    gw = make_engine()
    lat = []
    statuses = {}
    t0 = time.perf_counter()
    for i in range(n_actions):
        a0 = time.perf_counter()
        rep = gw.execute({
            "actions": [{"tool": "type", "ref": 1,
                         "text": f"user{i}@bench.shop"}],
            "task_id": f"bench_task_{i}",
            "tab_id": "t1",
            "page_url": "https://bench.shop/x",
        })
        lat.append((time.perf_counter() - a0) * 1000.0)
        st = rep["transaction_status"]
        statuses[st] = statuses.get(st, 0) + 1
    total_s = time.perf_counter() - t0
    return {
        "data_source": "MEASURED",
        "path": ("ExecutionGateway.execute -> TransactionEngine strict: "
                 "validate > lease > policy > dispatch > ack > observe > "
                 "verify (extension mode, no browser)"),
        "n_actions": n_actions,
        "actions_per_sec": round(n_actions / total_s, 2),
        "mean_latency_ms": round(mean(lat), 3),
        "p50_latency_ms": round(percentile(lat, 50), 3),
        "p99_latency_ms": round(percentile(lat, 99), 3),
        "terminal_statuses": statuses,
        "note": ("actions end UNVERIFIED: no independent browser observation "
                 "in this environment. The timing measures harness overhead."),
    }


# -- 2. verification accuracy ---------------------------------------------------------

_EID = [0]


def _ev(evidence_type, payload, action_id="a1", task_id="t"):
    _EID[0] += 1
    return Evidence(evidence_id=f"e_bench_{_EID[0]}",
                    evidence_type=evidence_type, source="runtime",
                    timestamp=time.time(), action_id=action_id,
                    task_id=task_id, payload=payload)


def _claim(cid, kind, target=None, value=None, url=None, tool_name=None,
           claimed=None):
    pc = {"kind": kind}
    if target:
        pc["target"] = target
    if value is not None:
        pc["value"] = value
    if url:
        pc["url"] = url
    if tool_name:
        pc["tool_name"] = tool_name
    return Claim(claim_id=cid, task_id="t", actor="agent",
                 claim_type=CLAIM_TYPE_STATE, target=target or url or "?",
                 requested_state=claimed, claimed_state=claimed,
                 action_ids=["a1"], postcondition=pc)


def bench_verification_accuracy() -> dict:
    """COMPUTED: known claim/evidence pairs through the real Verifier."""
    pairs = [
        # (name, claim, [evidence], expected)
        ("element_value match",
         _claim("c1", "element_value", target="#email", value="a@b.c",
                claimed="a@b.c"),
         [_ev(ELEMENT_STATE, {"target": "#email", "value": "a@b.c"})],
         VERIFIED),
        ("element_value mismatch -> UNVERIFIED (no confirming observation)",
         _claim("c2", "element_value", target="#email", value="a@b.c",
                claimed="a@b.c"),
         [_ev(ELEMENT_STATE, {"target": "#email", "value": "other@x.y"})],
         UNVERIFIED),
        ("element_value contradicted by DOM_CHANGE -> CONFLICTING",
         _claim("c3", "element_value", target="#email", value="a@b.c",
                claimed="a@b.c"),
         [_ev(DOM_CHANGE, {"state": "zzz"})],
         CONFLICTING),
        ("no evidence -> UNVERIFIED",
         _claim("c4", "element_value", target="#email", value="a@b.c",
                claimed="a@b.c"),
         [], UNVERIFIED),
        ("BROWSER_EVENT alone never verifies",
         _claim("c5", "element_value", target="#email", value="a@b.c",
                claimed="a@b.c"),
         [_ev(BROWSER_EVENT, {"command": "type"})], UNVERIFIED),
        ("BROWSER_ACK alone never verifies",
         _claim("c6", "element_value", target="#email", value="a@b.c",
                claimed="a@b.c"),
         [_ev(BROWSER_ACK, {"executed": True})], UNVERIFIED),
        ("bare screenshot never verifies",
         _claim("c7", "element_value", target="#email", value="a@b.c",
                claimed="a@b.c"),
         [_ev(SCREENSHOT, {"png": "bytes"})], UNVERIFIED),
        ("vision-verified screenshot verifies",
         _claim("c8", "element_value", target="#email", value="a@b.c",
                claimed="a@b.c"),
         [_ev(SCREENSHOT, {"vision_verified": True, "target": "#email",
                           "value": "a@b.c"})],
         VERIFIED),
        ("url match",
         _claim("c9", "url", url="https://bench.shop/done",
                claimed="https://bench.shop/done"),
         [_ev(URL_CHANGE, {"url": "https://bench.shop/done"})],
         VERIFIED),
        ("url contradiction -> CONFLICTING",
         _claim("c10", "url", url="https://bench.shop/done",
                claimed="https://bench.shop/done"),
         [_ev(URL_CHANGE, {"url": "https://bench.shop/login"})],
         CONFLICTING),
        ("url wrong observation contradicts -> CONFLICTING",
         _claim("c11", "url", url="https://bench.shop/done",
                claimed="https://bench.shop/done"),
         [_ev(URL_CHANGE, {"url": "https://bench.shop/other"})],
         CONFLICTING),
        ("webmcp ok=True verifies",
         _claim("c12", "webmcp_result", tool_name="searchProducts"),
         [_ev(WEBMCP_RESULT, {"tool": "searchProducts", "ok": True})],
         VERIFIED),
        ("webmcp ok=False -> FAILED",
         _claim("c13", "webmcp_result", tool_name="searchProducts"),
         [_ev(WEBMCP_RESULT, {"tool": "searchProducts", "ok": False,
                              "error": "timeout"})],
         FAILED),
        ("webmcp fixture fallback never verifies",
         _claim("c14", "webmcp_result", tool_name="searchProducts"),
         [_ev(WEBMCP_RESULT, {"tool": "searchProducts", "ok": True,
                              "partial_fallback": True})],
         UNVERIFIED),
        ("webmcp wrong tool name -> UNVERIFIED",
         _claim("c15", "webmcp_result", tool_name="searchProducts"),
         [_ev(WEBMCP_RESULT, {"tool": "otherTool", "ok": True})],
         UNVERIFIED),
        ("unknown postcondition kind fails closed",
         _claim("c16", "teleport_to_mars", target="#email", claimed="x"),
         [_ev(ELEMENT_STATE, {"target": "#email", "value": "x"})],
         UNVERIFIED),
        ("element_interaction with DOM observation verifies",
         _claim("c17", "element_interaction", target="#submit",
                claimed="clicked"),
         [_ev(DOM_CHANGE, {"target": "#submit", "state": "clicked"})],
         VERIFIED),
        ("element_interaction with only BROWSER_EVENT -> UNVERIFIED",
         _claim("c18", "element_interaction", target="#submit",
                claimed="#submit"),
         [_ev(BROWSER_EVENT, {"command": "click"})], UNVERIFIED),
        ("human confirmation verifies element_value",
         _claim("c19", "element_value", target="#email", value="a@b.c",
                claimed="a@b.c"),
         [_ev(HUMAN_EVENT, {"target": "#email", "value": "a@b.c",
                            "confirmed": True})],
         VERIFIED),
    ]
    correct = 0
    details = []
    for name, claim, evs, expected in pairs:
        v = Verifier(WorldState())
        v.propose_claim(claim)
        for e in evs:
            v.record_evidence(e)
        got = v.verify(claim.claim_id).result
        ok = got == expected
        correct += ok
        details.append({"pair": name, "expected": expected, "got": got,
                        "correct": ok})
    return {
        "data_source": "COMPUTED",
        "path": "real Verifier.verify() over declared claim/evidence pairs",
        "n_pairs": len(pairs),
        "correct": correct,
        "accuracy": round(correct / len(pairs), 4),
        "pairs": details,
    }


# -- 3. lease contention ----------------------------------------------------------------

def bench_lease_contention(n_threads: int = 8, attempts: int = 50) -> dict:
    """MEASURED: threads race for one exclusive lease; exclusivity asserted."""
    og = OwnershipGraph()
    lm = LeaseManager(og)
    inside = 0
    violations = 0
    lock = threading.Lock()
    acquired = 0
    refused = 0
    lat = []
    lat_lock = threading.Lock()

    def worker(tid):
        nonlocal inside, violations, acquired, refused
        for _ in range(attempts):
            a0 = time.perf_counter()
            lease = lm.acquire("tab:bench", intent="bench",
                               task_id=f"worker_{tid}", ttl=5.0)
            dt = (time.perf_counter() - a0) * 1000.0
            with lat_lock:
                lat.append(dt)
            if lease is None:
                with lock:
                    refused += 1
                continue
            with lock:
                acquired += 1
                inside += 1
                if inside > 1:
                    violations += 1
            time.sleep(0.0005)  # hold briefly to widen the race window
            with lock:
                inside -= 1
            lm.release("tab:bench", lease["lease"])

    threads = [threading.Thread(target=worker, args=(i,))
               for i in range(n_threads)]
    t0 = time.perf_counter()
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    total_s = time.perf_counter() - t0
    return {
        "data_source": "MEASURED",
        "path": "real LeaseManager.acquire/release under thread contention",
        "threads": n_threads,
        "attempts_per_thread": attempts,
        "acquired": acquired,
        "refused": refused,
        "exclusivity_violations": violations,
        "exclusivity_held": violations == 0,
        "mean_attempt_latency_ms": round(mean(lat), 4),
        "p99_attempt_latency_ms": round(percentile(lat, 99), 4),
        "wall_s": round(total_s, 3),
    }


# -- 4. policy latency --------------------------------------------------------------------

def bench_policy_latency(n_calls: int = 2000) -> dict:
    """MEASURED: per-call latency of the full check_action path."""
    events = []
    pol = OriginPolicyRegistry(emit=lambda t, d: events.append(t))
    pol.register_origin("bench.shop",
                        allow=["read", "navigate", "interact", "webmcp"],
                        require_consent=["destructive"],
                        description="benchmark origin")
    lat = []
    allowed = 0
    for i in range(n_calls):
        a0 = time.perf_counter()
        d = pol.check_action(f"task_{i % 8}", "bench.shop", "interact")
        lat.append((time.perf_counter() - a0) * 1_000_000.0)  # us
        allowed += d.allowed
    return {
        "data_source": "MEASURED",
        "path": "OriginPolicyRegistry.check_action (decision + escalation)",
        "n_calls": n_calls,
        "allowed": allowed,
        "mean_latency_us": round(mean(lat), 2),
        "p50_latency_us": round(percentile(lat, 50), 2),
        "p99_latency_us": round(percentile(lat, 99), 2),
    }


# -- 5. scheduler dispatch ------------------------------------------------------------------

def bench_scheduler_dispatch() -> dict:
    """COMPUTED + MEASURED: dispatch order correctness + dispatch latency."""
    og = OwnershipGraph()
    lm = LeaseManager(og)
    sched = TaskScheduler(leases=lm)
    order = []
    lat = []

    def exec_fn(task):
        order.append(task["task_id"])
        return {"ok": True}

    # Diamond: A -> B, A -> C, B+C -> D; plus independent E, F (FIFO).
    sched.submit({"task_id": "A", "goal": "a", "tab_id": "t1",
                  "actions": [{"tool": "click", "target": "#x"}]})
    sched.submit({"task_id": "B", "goal": "b", "tab_id": "t2",
                  "depends_on": ["A"],
                  "actions": [{"tool": "click", "target": "#x"}]})
    sched.submit({"task_id": "C", "goal": "c", "tab_id": "t3",
                  "depends_on": ["A"],
                  "actions": [{"tool": "click", "target": "#x"}]})
    sched.submit({"task_id": "D", "goal": "d", "tab_id": "t4",
                  "depends_on": ["B", "C"],
                  "actions": [{"tool": "click", "target": "#x"}]})
    sched.submit({"task_id": "E", "goal": "e", "tab_id": "t5",
                  "actions": [{"tool": "click", "target": "#x"}]})
    sched.submit({"task_id": "F", "goal": "f", "tab_id": "t6",
                  "actions": [{"tool": "click", "target": "#x"}]})

    dispatched = []
    while True:
        a0 = time.perf_counter()
        res = sched.run_next(exec_fn)
        lat.append((time.perf_counter() - a0) * 1000.0)
        if res is None:
            break
        dispatched.append(res["task_id"])
        if len(dispatched) > 10:
            break

    pos = {t: i for i, t in enumerate(order)}
    checks = {
        "A_before_B": pos["A"] < pos["B"],
        "A_before_C": pos["A"] < pos["C"],
        "B_before_D": pos["B"] < pos["D"],
        "C_before_D": pos["C"] < pos["D"],
        "E_before_F_fifo": pos["E"] < pos["F"],
        "all_six_dispatched": sorted(order) == ["A", "B", "C", "D", "E", "F"],
    }
    # Tab-lease blocking: a task whose tab is held by a foreign lease
    # must report BLOCKED, not run.
    sched2 = TaskScheduler(leases=lm)
    foreign = lm.acquire("tab:busy", task_id="foreign")
    sched2.submit({"task_id": "G", "goal": "g", "tab_id": "busy",
                   "actions": [{"tool": "click", "target": "#x"}]})
    blocked_res = sched2.run_next(exec_fn)
    checks["foreign_tab_lease_blocks"] = (
        blocked_res is not None and blocked_res.get("status") == BLOCKED)
    lm.release("tab:busy", foreign["lease"])
    # After release the task dispatches.
    unblocked = sched2.run_next(exec_fn)
    checks["released_tab_dispatches"] = (
        unblocked is not None and unblocked.get("status") == COMPLETED)

    return {
        "data_source": "COMPUTED + MEASURED",
        "path": ("real TaskScheduler.run_next with dependency graph and "
                 "foreign tab-lease blocking"),
        "dispatch_order": order,
        "correctness_checks": checks,
        "all_correct": all(checks.values()),
        "n_dispatched": len(dispatched),
        "mean_dispatch_latency_ms": round(mean(lat), 4),
        "p99_dispatch_latency_ms": round(percentile(lat, 99), 4),
    }


# -- runner -----------------------------------------------------------------------------------

BENCHMARKS = [
    ("gateway_throughput", bench_gateway_throughput),
    ("verification_accuracy", bench_verification_accuracy),
    ("lease_contention", bench_lease_contention),
    ("policy_latency", bench_policy_latency),
    ("scheduler_dispatch", bench_scheduler_dispatch),
]


def run_all(verbose: bool = True) -> dict:
    results = {}
    for name, fn in BENCHMARKS:
        if verbose:
            print(f"--- {name} ...", flush=True)
        t0 = time.perf_counter()
        try:
            out = fn()
            out["benchmark_ok"] = True
        except Exception as e:  # noqa: BLE001 - a failed benchmark is data
            out = {"benchmark_ok": False, "error": f"{type(e).__name__}: {e}"}
        out["wall_s"] = round(time.perf_counter() - t0, 3)
        results[name] = out
        if verbose:
            print(f"    ok={out['benchmark_ok']} "
                  f"wall={out['wall_s']}s", flush=True)
    return {
        "generated_by": "benchmark/runner.py (SYNK 2.0 Stage H)",
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "python": sys.version.split()[0],
        "benchmarks": results,
    }


def save_results(results: dict,
                 path: str = "benchmark/results.json") -> str:
    out = os.path.join(str(Path(__file__).resolve().parent.parent), path) \
        if not os.path.isabs(path) else path
    os.makedirs(os.path.dirname(out), exist_ok=True)
    with open(out, "w") as f:
        json.dump(results, f, indent=2)
    return out


if __name__ == "__main__":
    res = run_all(verbose=True)
    dest = save_results(res)
    print(f"\nwrote {dest}")
    print(json.dumps(
        {k: {kk: vv for kk, vv in v.items() if kk != "pairs"}
         for k, v in res["benchmarks"].items()}, indent=2))
