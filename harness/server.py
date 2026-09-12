"""Local harness server (stdlib only): Alpha API + Beta co-execution runtime.

Alpha endpoints (unchanged): /health /snapshot /plan /act /human
  /memory /memory/pref /memory/forget /audit
Beta endpoints: /event /world /lease /compile /transact
  /workflow/observe /workflow/suggest /ladder /benchmark

Run:  python server.py [--port 18080] [--db agent_memory.db]
"""
from __future__ import annotations

import argparse
import json
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import urlparse

from .benchmarks import suite as bench_suite
from .compiler import compile_intent, ir_to_actions
from .concurrency import (CONFLICT, HUMAN_OWNED, LeaseManager,
                              OwnershipGraph, TransactionRunner)
from .context_manager import ContextManager
from .event_bus import EventBus
from .ladder import ExecutionLadder, LADDER
from .memory import MemoryStore
from .orchestrator import Orchestrator
from .router import pick_tier, tier_to_model_label
from .safety import SafetyConfig, SafetyLayer
from .tools import ToolExecutor
from .webmcp import (WebMCPDiscovery, WebMCPAdapter, REGISTRY,
                      CAPABILITY_POLICY, PolicyEngine)
from .workflows import WorkflowMemory, WorkflowMiner, classify
from .world_state import WorldState
from .local.runtime import LocalRuntime
from .scheduler.scheduler import ParallelScheduler
from .verification.verifier import Verifier
from .verification.claims import Claim
from .verification.evidence import (
    Evidence, BROWSER_EVENT, DOM_CHANGE, NAVIGATION, SCREENSHOT, URL_CHANGE,
)
from .world_state import WorldState
from .browser import BrowserController
import asyncio
import threading
import uuid



def _domain_of(url: str) -> str:
    try:
        return url.split("//", 1)[1].split("/", 1)[0].lower()
    except IndexError:
        return ""


def _normalize_local_decision(decision: dict, goal: str = ""):
    """Map a local-runtime decision to the fixed tool allowlist.

    Returns a valid action dict, or None when the decision must escalate
    to the cloud/mock planner instead of emitting an unknown tool.
    """
    from .tools import TOOL_SCHEMAS as _SCHEMAS
    _alias = {"fill": "type"}
    raw = decision.get("decision", "click")
    tool = _alias.get(raw, raw)
    if tool not in _SCHEMAS or tool == "bulk":
        return None
    action = {"tool": tool, "intent": goal}
    if decision.get("ref") is not None:
        action["ref"] = decision.get("ref")
    for k in ("text", "value", "url", "selector", "direction", "key"):
        if decision.get(k) is not None:
            action[k] = decision.get(k)
    return action


def _claimed_state_for_action(action: dict):
    """Derive a concrete claimed_state for common tools, else None."""
    tool = action.get("tool", action.get("action", ""))
    if tool == "navigate":
        return action.get("url")
    if tool == "type":
        return action.get("text")
    if tool == "select":
        return action.get("value")
    if tool in ("click", "hover", "focus"):
        return action.get("target", action.get("selector", action.get("ref")))
    return None


class State:
    def __init__(self, db_path: str, use_cdp: bool = False):
        self.safety = SafetyLayer(SafetyConfig())
        self.ctx = ContextManager()
        self.mem = MemoryStore(db_path)
        # Phase 2: CDP Browser Controller (must exist before ToolExecutor).
        # Default off: extension push mode. Enable with use_cdp=True.
        self.use_cdp = use_cdp
        self.browser = BrowserController(headless=False)
        self.loop = asyncio.new_event_loop()
        self.browser_thread = None
        if self.use_cdp:
            self.browser_thread = threading.Thread(target=self._run_event_loop, daemon=True)
            self.browser_thread.start()
        self.tools = ToolExecutor(self.safety, browser=self.browser)
        self.llm = Orchestrator()


        # Beta runtime: world + bus + ownership + transactions + learning
        self.bus = EventBus()
        self.world = WorldState()
        self.ownership = OwnershipGraph()
        self.leases = LeaseManager(self.ownership)
        self.tx = TransactionRunner(self.world, self.ownership,
                                    self.leases, self.tools, self.safety)
        self.miner = WorkflowMiner()
        self.wfmem = WorkflowMemory(":memory:")
        # Beta.1: WebMCP integration
        self.webmcp_discovery = WebMCPDiscovery()
        self.webmcp_adapter = WebMCPAdapter(discovery=self.webmcp_discovery,
                                              ownership=self.ownership,
                                              leases=self.leases,
                                              safety=self.safety)
        # Beta.2: Local Intelligence Runtime
        self.local_runtime = LocalRuntime()
        # Beta.3: Parallel Scheduler
        self.scheduler = ParallelScheduler(self.ownership)
        # Beta.2 Truth Layer: Independent Verifier
        self.verifier = Verifier(self.world)
        # Use the ladder with WebMCP integration
        self.ladder = ExecutionLadder(webmcp_adapter=self.webmcp_adapter)

        self.bus.subscribe("*", self._on_event)

    def _run_event_loop(self):
        asyncio.set_event_loop(self.loop)
        self.loop.run_until_complete(self.browser.start())
        self.loop.run_forever()

    # -- bus fan-out -----------------------------------------------------------
    def _on_event(self, ev: dict) -> None:

        t, d = ev["type"], ev["data"]
        if t in ("page.loaded", "page.navigated", "dom.changed", "value.changed",
                 "focus.changed", "human.action", "agent.action", "agent.lease",
                 "agent.goal", "dialog.opened", "dialog.closed"):
            self.world.apply_event({"type": t, "data": d})
        # Truth Layer: runtime observations become immutable Evidence.
        try:
            task_id = d.get("task_id")
            action_id = d.get("action_id")
            if t in ("page.loaded", "page.navigated") and d.get("url"):
                self.verifier.record_evidence(Evidence(
                    evidence_id=f"e_{uuid.uuid4().hex[:8]}",
                    evidence_type=URL_CHANGE if t == "page.loaded" else NAVIGATION,
                    source="runtime", timestamp=time.time(),
                    action_id=action_id, task_id=task_id,
                    world_state_version=self.world.version,
                    payload={"url": d.get("url"), "title": d.get("title", ""),
                             "tab_id": d.get("tab_id", "default")},
                ))
            elif t in ("dom.changed", "value.changed") and task_id:
                self.verifier.record_evidence(Evidence(
                    evidence_id=f"e_{uuid.uuid4().hex[:8]}",
                    evidence_type=DOM_CHANGE,
                    source="runtime", timestamp=time.time(),
                    action_id=action_id, task_id=task_id,
                    world_state_version=self.world.version,
                    payload={"target": d.get("target"),
                             "state": d.get("detail", d.get("value")),
                             "tab_id": d.get("tab_id", "default")},
                ))
        except Exception:
            pass
        if t == "human.action":
            if d.get("target"):
                self.ownership.mark_human(d["target"])
            step = classify({"tool": d.get("kind", "?"), "target": d.get("target", "?"),
                             "domain": _domain_of(d.get("url", ""))})
            self._human_steps.append(step)

    _human_steps: list = []  # per-process recent human steps for the miner


STATE = State(":memory:")
STATE._human_steps = []


class Handler(BaseHTTPRequestHandler):
    server_version = "CoworkHarness/0.2"

    def _send(self, code: int, obj: dict) -> None:
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _body(self) -> dict:
        try:
            n = int(self.headers.get("Content-Length", 0))
        except ValueError:
            n = 0
        if not n:
            return {}
        try:
            return json.loads(self.rfile.read(n).decode())
        except Exception:
            return {}

    def log_message(self, *a):  # quieter logs
        pass

    # -- GET --------------------------------------------------------------------
    def do_GET(self):
        path = urlparse(self.path).path
        if path == "/health":
            return self._send(200, {"ok": True, "service": "ai-cowork-harness",
                                    "version": "0.2-beta"})
        if path == "/memory":
            return self._send(200, {"summary": STATE.mem.summary_for_prompt(),
                                    "prefs": STATE.mem.all_prefs(),
                                    "recent": STATE.mem.recent(10)})
        if path == "/audit":
            return self._send(200, {"trail": STATE.safety.audit_trail(),
                                    "chain_valid": STATE.safety.verify_chain()})
        if path == "/world":
            return self._send(200, {"world": STATE.world.snapshot(),
                                    "ownership": STATE.ownership.snapshot(),
                                    "events": STATE.bus.recent(30)})
        if path == "/ladder":
            return self._send(200, {"levels": STATE.ladder.describe()})
        if path == "/workflows":
            return self._send(200, {"workflows": STATE.wfmem.all_workflows()})
        # Beta.1: WebMCP GET = list-only, no body
        if path == "/webmcp/capabilities":
            return self._webmcp_capabilities({})
        return self._send(404, {"ok": False, "error": "not found"})

    # -- POST -------------------------------------------------------------------
    def do_POST(self):
        path = urlparse(self.path).path
        b = self._body()
        if path == "/snapshot":
            return self._snapshot(b)
        if path == "/plan":
            return self._plan(b)
        if path == "/act":
            return self._act(b)
        if path == "/transact":
            return self._transact(b)
        if path == "/event":
            ev = STATE.bus.emit(b.get("type", "unknown"), b.get("data", {}))
            return self._send(200, {"ok": True, "event": ev,
                                    "world_version": STATE.world.version})
        if path == "/lease":
            return self._lease(b)
        if path == "/compile":
            ir = compile_intent(b.get("intent", ""), b.get("slots", {}))
            return self._send(200, {"ir": ir, "actions": ir_to_actions(ir)})
        if path == "/human":
            STATE.tools.set_human_active(bool(b.get("active", True)))
            return self._send(200, {"paused_for_user": STATE.tools.paused_for_user})
        if path == "/memory/pref":
            STATE.mem.set_pref(b.get("key", ""), b.get("value", ""))
            return self._send(200, {"ok": True})
        if path == "/memory/forget":
            STATE.mem.forget_all()
            return self._send(200, {"ok": True, "forgotten": True})
        if path == "/workflow/observe":
            return self._workflow_observe(b)
        if path == "/workflow/suggest":
            domain = b.get("domain", "")
            prep = STATE.wfmem.prepare(domain, b.get("intent", ""))
            tier, reason = pick_tier(b.get("intent", ""), workflow_matched=prep["matched"])
            return self._send(200, {"preparation": prep, "tier": tier,
                                    "tier_reason": reason})
        if path == "/benchmark":
            return self._send(200, _run_benchmarks())
        # Beta.3: Parallel Task Submission + plan-only execution
        if path == "/task/submit":
            tid = b.get("task_id", "t1")
            tab = b.get("tab_id", "default")
            intent = b.get("intent", "")
            deps = set(b.get("dependencies", []))
            try:
                STATE.scheduler.submit(tid, tab, intent, deps)
            except ValueError as e:
                return self._send(409, {"ok": False, "error": str(e)})
            return self._send(200, {"ok": True, "task_id": tid,
                                    "ready": len(STATE.scheduler.get_ready_tasks())})
        if path == "/task/poll":
            tid = b.get("task_id", "t1")
            task = STATE.scheduler.tasks.get(tid)
            if not task: return self._send(404, {"error": "not found"})
            return self._send(200, {"status": task.status, "result": task.result,
                                    "tab_id": task.tab_id,
                                    "dependencies": sorted(task.dependencies)})
        if path == "/task/run":
            def _plan_task(task):
                prompt = STATE.ctx.build_prompt(task.intent, STATE.mem.summary_for_prompt())
                prompt += "\n" + STATE.world.prompt_section()
                plan = STATE.llm.plan(task.intent, STATE.safety.mask_pii(prompt),
                                      STATE.mem.summary_for_prompt())
                return {"intent": task.intent, "tab_id": task.tab_id, "plan": plan}
            ran = STATE.scheduler.run_ready(_plan_task)
            return self._send(200, {"ok": True,
                                    "ran": [{"task_id": t.task_id, "status": t.status,
                                             "tab_id": t.tab_id} for t in ran]})
        # Beta.2 Truth Layer endpoints
        if path == "/verification/claim":
            from .verification.claims import Claim
            claim = Claim(
                claim_id=b.get("claim_id", "c1"),
                task_id=b.get("task_id", "t1"),
                actor=b.get("actor", "agent"),
                claim_type=b.get("claim_type", "STATE_CHANGE"),
                target=b.get("target", ""),
                requested_state=b.get("requested_state"),
                claimed_state=b.get("claimed_state"),
                action_ids=b.get("action_ids", [])
            )
            STATE.verifier.propose_claim(claim)
            return self._send(200, {"ok": True, "claim_id": claim.claim_id})
        if path == "/verification/verify":
            cid = b.get("claim_id", "c1")
            res = STATE.verifier.verify(cid)
            return self._send(200, res.to_dict())
        if path == "/verification/evidence":
            tid = b.get("task_id", "t1")
            evs = STATE.verifier.get_evidence_for_task(tid)
            return self._send(200, {"evidence": [e.to_dict() for e in evs]})
        # L3 selective vision: capture a screenshot as immutable Evidence.
        # CDP mode captures directly; extension mode accepts pushed PNG base64.
        if path == "/vision/capture":
            import base64
            import hashlib
            tab_id = b.get("tab_id", "default")
            task_id = b.get("task_id")
            png = None
            if STATE.use_cdp:
                fut = asyncio.run_coroutine_threadsafe(
                    STATE.browser.screenshot(tab_id), STATE.loop)
                png = fut.result(timeout=15)
            elif b.get("image_base64"):
                png = base64.b64decode(b["image_base64"])
            else:
                return self._send(400, {"ok": False, "error":
                    "no browser source: enable --use-cdp or push image_base64"})
            digest = hashlib.sha256(png).hexdigest()
            ev = Evidence(
                evidence_id=f"e_{uuid.uuid4().hex[:8]}",
                evidence_type=SCREENSHOT,
                source="runtime", timestamp=time.time(),
                action_id=b.get("action_id"), task_id=task_id,
                world_state_version=STATE.world.version,
                payload={"sha256": digest, "bytes": len(png),
                         "tab_id": tab_id,
                         "image_base64": base64.b64encode(png).decode()
                         if len(png) <= 1_500_000 else None},
            )
            STATE.verifier.record_evidence(ev)
            STATE.bus.emit("page.screenshot", {"sha256": digest, "tab_id": tab_id,
                                               "task_id": task_id})
            d = ev.to_dict()
            if d["payload"]["image_base64"] is None:
                d["payload"]["image_base64"] = "<omitted: >1.5MB>"
            return self._send(200, {"ok": True, "evidence": d})
        # Beta.1: WebMCP endpoints
        if path == "/webmcp/discover":
            return self._webmcp_discover(b)
        if path == "/webmcp/capabilities":
            return self._webmcp_capabilities(b)
        if path == "/webmcp/execute":
            return self._webmcp_execute(b)
        return self._send(404, {"ok": False, "error": "not found"})

    # -- handlers ------------------------------------------------------------------
    def _snapshot(self, b: dict):
        tab_id = b.get("tab_id", "default")
        title = b.get("title", "")
        if STATE.use_cdp:
            # Phase 2: CDP pull mode
            future = asyncio.run_coroutine_threadsafe(
                STATE.browser.snapshot(tab_id), STATE.loop
            )
            snap = future.result(timeout=15)
            url = snap["url"]
            nodes = snap["nodes"]
            title = snap.get("title", "")
        else:
            # Extension push mode (default): body carries url/nodes
            url = b.get("url", "")
            nodes = b.get("nodes", [])
        for i, n in enumerate(nodes):
            n.setdefault("index", i)
        flat = json.dumps(nodes)[:20000]
        injected = STATE.safety.detect_injection(flat)
        view = STATE.ctx.ingest(url, nodes, b.get("goal", ""), b.get("screenshot_note", ""))
        STATE.world.load_full(url, view["nodes"], title, tab_id=tab_id)
        STATE.bus.emit("page.loaded", {"url": url, "title": title, "tab_id": tab_id,
                                       "task_id": b.get("task_id"),
                                       "action_id": b.get("action_id")})
        view["injection_suspected"] = injected
        view["world_version"] = STATE.world.version
        view["world"] = STATE.world.prompt_section()
        view["prompt"] = STATE.ctx.build_prompt(b.get("goal", ""), STATE.mem.summary_for_prompt())
        view["prompt"] = STATE.safety.mask_pii(view["prompt"])
        if injected:
            view["warning"] = ("Page contains possible prompt-injection text; "
                                "treated as UNTRUSTED data.")
        return self._send(200, view)


    def _plan(self, b: dict):
        goal = b.get("goal", "")
        url = STATE.world.tabs.get(STATE.world.active_tab, {}).get("url", "")
        domain = _domain_of(url)
        prep = STATE.wfmem.prepare(domain, goal)
        tier, tier_reason = pick_tier(goal, workflow_matched=prep["matched"])
        level, level_reason, _ = STATE.ladder.choose_level(
            domain, goal, vision_available=STATE.use_cdp)
        
        # Beta.2: Local Decision Path
        start_time = time.time()
        page_state = getattr(STATE.world, "page", None)
        if page_state is None:
            # WorldState uses tabs dict; fall back to active tab
            page_state = STATE.world.tabs.get(STATE.world.active_tab, {})
        state_sig = f"v{STATE.world.version}:{hash(json.dumps(page_state, sort_keys=True, default=str))}"
        decision, routing_type = STATE.local_runtime.decide(
            site=domain, state_sig=state_sig, intent=goal,
            workflow_id=prep.get("workflow_id")
        )
        
        # Local fast path: only when the decision maps to the fixed tool allowlist.
        action = None
        if routing_type != 'cloud_llm' and decision.get("decision") != "escalate":
            action = _normalize_local_decision(decision, goal)
        if action is not None:
            plan = {"actions": [action]}
            plan["routing"] = routing_type
            plan["tier"] = "local-slm"
            plan["tier_reason"] = f"routed via {routing_type}"
            plan["execution_level"] = level
            plan["execution_reason"] = level_reason
            # Beta.2: Add telemetry to the plan for benchmark tracking
            plan["telemetry"] = {
                "routing_type": routing_type,
                "confidence": decision.get("confidence", 0.0),
                "latency_ms": (time.time() - start_time) * 1000,
                "tokens_saved": 200 if routing_type == 'local_slm' else 500,
                "cost_saved": 0.005 if routing_type == 'local_slm' else 0.01,
            }
            return self._send(200, plan)

        # Fallback to Cloud LLM
        prompt = STATE.ctx.build_prompt(goal, STATE.mem.summary_for_prompt())
        prompt += "\n" + STATE.world.prompt_section()
        plan = STATE.llm.plan(goal, STATE.safety.mask_pii(prompt),
                               STATE.mem.summary_for_prompt())
        plan["tier"] = tier
        plan["tier_reason"] = tier_reason
        plan["model_routed"] = tier_to_model_label(tier)
        plan["execution_level"] = level
        plan["execution_reason"] = level_reason
        if prep["matched"]:
            plan["workflow_preparation"] = prep
        return self._send(200, plan)

    def _act(self, b: dict):
        tab_id = b.get("tab_id", "default")
        page_url = b.get("page_url", "")
        consented = bool(b.get("user_consented", False))
        task_id = b.get("task_id") or f"t_{uuid.uuid4().hex[:8]}"
        claim_id = b.get("claim_id") or f"c_{uuid.uuid4().hex[:8]}"
        actions = b.get("actions") or ([b["action"]] if "action" in b else [])
        action_ids = [f"{claim_id}:{i}" for i in range(len(actions))]
        results = [STATE.tools.run(a, page_url, consented, tab_id=tab_id) for a in actions]
        now = time.time()
        for i, (a, r) in enumerate(zip(actions, results)):
            STATE.mem.learn_from_action(a)
            STATE.mem.record(page_url, a, json.dumps(r)[:500], b.get("note", ""))
            target = a.get("target", a.get("selector", a.get("ref")))
            STATE.bus.emit("agent.action", {"command": r.get("command", a.get("tool")),
                                            "target": target,
                                            "ok": r.get("ok"),
                                            "task_id": task_id,
                                            "action_id": action_ids[i],
                                            "tab_id": tab_id})
            # Honest tool-execution evidence: BROWSER_EVENT only.
            # Real DOM/URL confirmation must arrive via /snapshot or /event.
            STATE.verifier.record_evidence(Evidence(
                evidence_id=f"e_{uuid.uuid4().hex[:8]}",
                evidence_type=BROWSER_EVENT,
                source="runtime",
                timestamp=now,
                action_id=action_ids[i],
                task_id=task_id,
                world_state_version=STATE.world.version,
                payload={"command": a.get("tool"), "target": target,
                         "ok": r.get("ok"), "tab_id": tab_id},
            ))
        # One claim per /act batch; claimed_state from first action (or explicit override).
        claimed_state = b.get("claimed_state")
        if claimed_state is None and actions:
            claimed_state = _claimed_state_for_action(actions[0])
        STATE.verifier.propose_claim(Claim(
            claim_id=claim_id,
            task_id=task_id,
            actor="agent",
            claim_type="ACTION_COMPLETED",
            target=str(actions[0].get("target", actions[0].get("selector", actions[0].get("ref", "?")))) if actions else "?",
            requested_state=actions[0] if actions else {},
            claimed_state=claimed_state,
            action_ids=action_ids,
        ))
        verification = STATE.verifier.verify(claim_id).to_dict()
        STATE.bus.emit("agent.action_verified", {"claim_id": claim_id, "task_id": task_id,
                                                 "result": verification.get("result"),
                                                 "tab_id": tab_id})
        return self._send(200, {"results": results,
                                "task_id": task_id,
                                "claim_id": claim_id,
                                "verification": verification,
                                "paused_for_user": STATE.tools.paused_for_user})


    def _transact(self, b: dict):
        """Transactional co-execution: lease -> validate -> execute -> verify."""
        tab_id = b.get("tab_id", "default")
        page_url = b.get("page_url", "")
        consented = bool(b.get("user_consented", False))
        task_id = b.get("task_id") or f"t_{uuid.uuid4().hex[:8]}"
        claim_id = b.get("claim_id") or f"c_{uuid.uuid4().hex[:8]}"
        actions = b.get("actions") or ([b["action"]] if "action" in b else [])
        action_ids = [f"{claim_id}:{i}" for i in range(len(actions))]
        results = [STATE.tx.run(a, page_url, consented, tab_id=tab_id) for a in actions]
        conflicts = sum(1 for r in results if r.get("verdict") in ("replan", "request_ownership"))
        now = time.time()
        for i, (a, r) in enumerate(zip(actions, results)):
            STATE.mem.record(page_url, a, json.dumps(r)[:500], b.get("note", "tx"))
            target = a.get("target", a.get("selector", a.get("ref")))
            STATE.bus.emit("agent.action", {"command": a.get("tool"),
                                            "target": target, "ok": r.get("ok"),
                                            "verdict": r.get("verdict"),
                                            "task_id": task_id,
                                            "action_id": action_ids[i],
                                            "tab_id": tab_id})
            STATE.verifier.record_evidence(Evidence(
                evidence_id=f"e_{uuid.uuid4().hex[:8]}",
                evidence_type=BROWSER_EVENT,
                source="runtime", timestamp=now,
                action_id=action_ids[i], task_id=task_id,
                world_state_version=STATE.world.version,
                payload={"command": a.get("tool"), "target": target,
                         "ok": r.get("ok"), "verdict": r.get("verdict"),
                         "tab_id": tab_id},
            ))
        claimed_state = b.get("claimed_state")
        if claimed_state is None and actions:
            claimed_state = _claimed_state_for_action(actions[0])
        STATE.verifier.propose_claim(Claim(
            claim_id=claim_id, task_id=task_id, actor="agent",
            claim_type="ACTION_COMPLETED",
            target=str(actions[0].get("target", actions[0].get("selector", actions[0].get("ref", "?")))) if actions else "?",
            requested_state=actions[0] if actions else {},
            claimed_state=claimed_state, action_ids=action_ids,
        ))
        verification = STATE.verifier.verify(claim_id).to_dict()
        STATE.bus.emit("agent.action_verified", {"claim_id": claim_id, "task_id": task_id,
                                                 "result": verification.get("result"),
                                                 "tab_id": tab_id})
        return self._send(200, {"results": results, "conflicts": conflicts,
                                "task_id": task_id, "claim_id": claim_id,
                                "verification": verification,
                                "ownership": STATE.ownership.snapshot()})

    def _lease(self, b: dict):
        if b.get("release"):
            STATE.leases.release(b["release"])
            return self._send(200, {"ok": True, "released": b["release"]})
        lease = STATE.leases.acquire(b.get("target", ""), b.get("intent", ""),
                                     ttl=float(b.get("ttl", 2.0)))
        if lease is None:
            return self._send(409, {"ok": False,
                                    "error": "target human-owned or conflicted; replan or request ownership"})
        STATE.bus.emit("agent.lease", {"lease": lease["lease"], "target": lease["target"]})
        return self._send(200, {"ok": True, **lease})

    def _workflow_observe(self, b: dict):
        steps = b.get("steps", [])
        if steps:
            STATE.miner.observe(steps)
        confirmed = []
        for c in STATE.miner.candidates():
            if c["observed_runs"] >= 3 or (c["observed_runs"] >= 2 and b.get("confirm")):
                name = f"{c['domain'] or 'web'}/{abs(hash(tuple(c['steps']))) % 10000:04d}"
                STATE.wfmem.confirm(name, c["domain"], b.get("intent", c["steps"][0]),
                                    c["steps"], c["confidence"], c["observed_runs"])
                confirmed.append(name)
            elif c["observed_runs"] >= 2:
                # auto-bump already-known workflows on repeat sightings
                for w in STATE.wfmem.all_workflows():
                    if w["domain"] == c["domain"]:
                        STATE.wfmem.bump(w["name"])
        return self._send(200, {"ok": True,
                                "candidates": STATE.miner.candidates(),
                                "confirmed": confirmed})

    # Beta.1: WebMCP endpoints
    def _webmcp_discover(self, b: dict):
        origin = b.get("origin", "")
        if not origin:
            url = b.get("url", "")
            try:
                origin = url.split("//", 1)[1].split("/", 1)[0].lower()
            except IndexError:
                return self._send(400, {"ok": False, "error": "origin or url required"})
        site = STATE.webmcp_discovery.discover(origin)
        if site:
            return self._send(200, {"ok": True, "site": {
                "origin": site.origin,
                "tools": [t.to_dict() for t in site.tools],
                "discovered_at": site.discovered_at,
            }})
        return self._send(404, {"ok": False, "error": f"no WebMCP tools for {origin}"})

    def _webmcp_capabilities(self, b: dict):
        origin = b.get("origin", "")
        goal = b.get("goal", "")
        caps = REGISTRY.get_for_origin(origin) if origin else REGISTRY.all()
        if goal:
            caps = REGISTRY.find_capabilities(goal, origin)
        return self._send(200, {"ok": True, "capabilities": [
            {"source": c.source, "origin": c.origin, "name": c.name,
             "risk": c.risk, "latency_class": c.latency_class,
             "semantic": c.semantic, "mutating": c.mutating,
             "requires_user_consent": c.requires_user_consent,
             "description": c.description}
            for c in caps
        ]})

    def _webmcp_execute(self, b: dict):
        origin = b.get("origin", "")
        tool = b.get("tool", "")
        args = b.get("args", {})
        goal = b.get("goal", "")
        user_consented = bool(b.get("user_consented", False))
        if not origin or not tool:
            return self._send(400, {"ok": False, "error": "origin and tool required"})
        res = STATE.webmcp_adapter.execute(origin, tool, args, goal, user_consented)
        return self._send(200, {"ok": res.ok, "result": res.result, "error": res.error})


def _run_benchmarks() -> dict:
    """Headless Beta benchmark: form-fill, conflict avoidance, workflow reuse."""
    from .concurrency import LeaseManager as LM
    from .concurrency import OwnershipGraph as OG
    from .concurrency import TransactionRunner as TR

    def t_form_fill(**_):
        from .context_manager import ContextManager as CM
        cm = CM()
        nodes = [
            {"role": "textbox", "name": "Email", "tag": "input",
             "selector": "#email", "interactive": True, "index": 0},
            {"role": "button", "name": "Submit", "tag": "button",
             "selector": "#s", "interactive": True, "index": 1},
        ]
        view = cm.ingest("https://bench.shop/x", nodes, "fill form")
        from .orchestrator import Orchestrator as O
        plan = O().plan("fill the form", cm.build_prompt("fill the form"))
        n_actions = sum(len(a.get("actions", [a])) for a in plan["actions"])
        ok = any(a.get("tool") == "bulk" for a in plan["actions"])
        return {"success": 1.0 if ok else 0.0, "llm_calls": 1,
                "tokens": view["tokens_est"] + 200, "actions": n_actions,
                "context_bytes": len(json.dumps(view["nodes"])),
                "cost_usd": (view["tokens_est"] + 200) * 0.5 / 1e6}

    def t_conflict(**_):
        og, lm = OG(), None
        from .safety import SafetyLayer as SL
        from .tools import ToolExecutor as TE
        from .world_state import WorldState as WS
        og = OG()
        lm = LM(og)
        w = WS()
        tx = TR(w, og, lm, TE(SL()), SL())
        og.mark_human("#pay")  # human is on the pay button
        r = tx.run({"tool": "click", "target": "#pay"}, "https://bench.shop/pay")
        avoided = (not r.get("ok")) and r.get("verdict") in ("replan", "request_ownership")
        return {"success": 1.0 if avoided else 0.0, "llm_calls": 0,
                "tokens": 60, "actions": 0, "conflicts": 1,
                "cost_usd": 60 * 0.5 / 1e6}

    def t_workflow(**_):
        from .workflows import WorkflowMemory as WM
        from .workflows import WorkflowMiner as WMi
        m, wm = WMi(), WM(":memory:")
        seq = ["crm|click@search", "crm|type@name", "crm|click@result", "crm|click@copy"]
        for _ in range(3):
            m.observe(seq)
        cands = m.candidates()
        if cands:
            c = cands[0]
            wm.confirm("crm/lookup", "crm", "lookup_customer", c["steps"],
                       c["confidence"], c["observed_runs"])
        prep = wm.prepare("crm", "lookup_customer")
        return {"success": 1.0 if prep["matched"] else 0.0, "llm_calls": 0,
                "tokens": 80, "actions": 0, "cost_usd": 80 * 0.5 / 1e6}

    return bench_suite([("form_fill", t_form_fill),
                        ("conflict_avoidance", t_conflict),
                        ("workflow_reuse", t_workflow)])


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=18080)
    ap.add_argument("--db", default=":memory:")
    ap.add_argument("--use-cdp", action="store_true",
                    help="Enable CDP browser control (requires playwright). Default: extension mode.")
    args = ap.parse_args()
    global STATE
    STATE = State(args.db, use_cdp=args.use_cdp)
    STATE._human_steps = []
    srv = HTTPServer(("127.0.0.1", args.port), Handler)
    print(f"ai-cowork harness on http://127.0.0.1:{args.port} (db={args.db} use_cdp={args.use_cdp})")
    srv.serve_forever()


if __name__ == "__main__":
    main()
