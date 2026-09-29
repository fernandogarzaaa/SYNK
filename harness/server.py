"""Local harness server (stdlib only): Alpha API + Beta co-execution runtime.

Alpha endpoints (unchanged): /health /snapshot /plan /act /human
  /memory /memory/pref /memory/forget /audit
Beta endpoints: /event /world /lease /compile /transact
  /workflow/observe /workflow/suggest /ladder /benchmark

Run:  python server.py [--port 18080] [--db agent_memory.db]
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import urlparse

from .benchmarks import suite as bench_suite
from .concurrency import (LeaseManager,
                              OwnershipGraph, TransactionRunner)
from .context_manager import ContextManager
from .event_bus import EventBus
from .session import SessionManager, MAIN_FRAME
from .ladder import ExecutionLadder
from .memory import MemoryStore
from .orchestrator import Orchestrator
from .router import pick_tier, tier_to_model_label
from .safety import SafetyConfig, SafetyLayer
from .tools import ToolExecutor
from .webmcp import (WebMCPDiscovery, WebMCPAdapter, PolicyEngine)
from .workflows import WorkflowMemory, WorkflowMiner, classify
from .world_state import WorldState
from .local.runtime import LocalRuntime
from .scheduler.scheduler import ParallelScheduler
from .task_scheduler import TaskScheduler
from .workflow_store import WorkflowLearner
from .model_router import ModelRegistry, ModelRouter
from .compiler import compile_spec, CompileError, ir_to_actions
from .verification.verifier import Verifier
from .verification.claims import Claim
from .verification.evidence import (
    Evidence, DOM_CHANGE, NAVIGATION, SCREENSHOT, URL_CHANGE,
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
    for k in ("text", "value", "url", "selector", "direction", "key",
              "tool_name", "args"):
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
    def __init__(self, db_path: str, use_cdp: bool = False,
                 cdp_endpoint: str = "", profile_dir: str = "",
                 webmcp_fallback: bool = False):
        self.safety = SafetyLayer(SafetyConfig())
        # Stage D browser modes (exactly one):
        #   default            attached-extension: the user's own browser,
        #                      driven only via extension snapshots + the
        #                      /agent/* closed loop. SYNK never launches or
        #                      debugs anything here.
        #   --use-cdp          managed launch: SYNK-owned Chromium with a
        #                      persistent SYNK profile. NOT the user's browser.
        #   --cdp-endpoint URL explicit attach: connect to the CDP endpoint
        #                      YOU named. Never inferred, never the default.
        if use_cdp and cdp_endpoint:
            raise ValueError("--use-cdp and --cdp-endpoint are mutually "
                             "exclusive")
        self.use_cdp = use_cdp
        self.cdp_endpoint = cdp_endpoint
        self.browser = BrowserController(
            headless=False, profile_dir=profile_dir or None)
        # Stage D: the connected BrowserRuntime (managed-launch or explicit
        # attach; set once the browser thread finishes connect()).
        self.browser_runtime = None
        self.loop = asyncio.new_event_loop()
        self.browser_thread = None
        if self.use_cdp or self.cdp_endpoint:
            self.browser_thread = threading.Thread(
                target=self._run_event_loop, daemon=True)
            self.browser_thread.start()
        self.tools = ToolExecutor(self.safety, browser=self.browser)
        self.llm = Orchestrator()


        # Beta runtime: world + bus + ownership + transactions + learning
        self.bus = EventBus()
        # Stage F: memory and context-manager journal events flow through
        # the bus (memory.read/stored/deleted, security.quarantine).
        self.ctx = ContextManager(emit=self.bus.emit)
        self.mem = MemoryStore(db_path, emit=self.bus.emit)
        self.world = WorldState()
        self.sessions = SessionManager()
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
        # Stage E: real WebMCP gateway. Default transport is the
        # extension snapshot transport: attached-mode pages report their
        # model-context tools with each snapshot, and invocations in
        # attached mode go through the closed-loop extension path.
        # _run_event_loop() swaps in the CDP transport once a managed
        # browser is connected. The transport always reports the LIVE
        # page; no static fallback is wired here (opt-in only).
        from .webmcp.transport import ExtensionSnapshotTransport
        from .webmcp.gateway import WebMCPGateway
        from .webmcp.policy import PolicyEngine
        # scope key string "session|tab|frame|document" ->
        # {"tools": [...], "reason": str}. The document id in the key is
        # the generation key: a navigation changes it, so a stale entry
        # can never be returned for the new document.
        self.webmcp_snapshots: dict = {}
        snapshots = self.webmcp_snapshots
        # Stage E: fixture fallback is opt-in only (flag or
        # SYNK_WEBMCP_FALLBACK=1); default is fail closed.
        self._webmcp_fallback_opt_in = bool(
            webmcp_fallback or
            os.environ.get("SYNK_WEBMCP_FALLBACK"))
        self.webmcp_gateway = WebMCPGateway(
            transport=ExtensionSnapshotTransport(
                lambda scope_key: snapshots.get(scope_key)),
            sessions=self.sessions,
            policy_engine=PolicyEngine(self.ownership, self.safety,
                                       leases=self.leases),
            allow_fallback=self._webmcp_fallback_opt_in,
            emit=self.bus.emit)
        self.tools.webmcp_gateway = self.webmcp_gateway
        # Beta.2: Local Intelligence Runtime
        self.local_runtime = LocalRuntime()
        # Beta.3: Parallel Scheduler (legacy: plan-only, synchronous, kept
        # for import compatibility; new code uses task_scheduler below)
        self.scheduler = ParallelScheduler(self.ownership)
        # Stage G: honest SEQUENTIAL task scheduler (Phase 9). Tasks run
        # one at a time through the execution gateway with per-action
        # verification; the scheduler never claims concurrency.
        self.task_scheduler = TaskScheduler(self.leases)
        # Stage G: workflow learner (Phase 13). Learns parameterized
        # workflows only from fully-verified transactions.
        wf_db = ":memory:" if db_path == ":memory:" \
            else str(db_path) + ".workflows"
        self.workflow_learner = WorkflowLearner(wf_db)
        # Stage G: capability-based model router (Phase 14). Registry from
        # SYNK_MODELS / SYNK_MODELS_FILE; a bad registry fails loudly here.
        self.model_router = ModelRouter(ModelRegistry.from_env())
        # Stage G: recent gateway reports (bounded) so /workflows/learn can
        # learn from a verified transaction by task id.
        self.recent_reports: dict = {}
        # Beta.2 Truth Layer: Independent Verifier
        self.verifier = Verifier(self.world)
        self.tools.verifier = self.verifier
        # Stage C: honest transaction lifecycle + execution gateway +
        # closed-loop agent. ExecutionGateway.execute() is the single
        # execution entrypoint used by /act, /transact, the extension,
        # Tauri, and tests.
        from .transactions import TransactionEngine
        from .gateway import ExecutionGateway
        from .orchestrator import AgentLoop
        # Stage F: per-origin execution policy (default-deny). Unknown
        # origins fail closed at the transaction engine's VALIDATE stage,
        # before any lease or dispatch. Operators register origins via
        # POST /policy/origin.
        from .policy import OriginPolicyRegistry
        self.policy = OriginPolicyRegistry()
        self.engine = TransactionEngine(self.world, self.ownership,
                                        self.leases, self.tools, self.safety,
                                        self.ctx, self.verifier,
                                        emit=self.bus.emit,
                                        policy=self.policy,
                                        sessions=self.sessions)
        self.policy.emit = self.bus.emit
        # Stage F: memory stores/deletions are journaled (hashes, not
        # content) through the event bus.
        self.mem.emit = self.bus.emit
        self.gateway = ExecutionGateway(self.engine, mem=self.mem,
                                        emit=self.bus.emit)
        self.agent = AgentLoop(self, self.llm)
        # Use the ladder with WebMCP integration
        self.ladder = ExecutionLadder(webmcp_adapter=self.webmcp_adapter)

        self.bus.subscribe("*", self._on_event)

    def _run_event_loop(self):
        asyncio.set_event_loop(self.loop)
        self.loop.run_until_complete(self.browser.start())
        # Expose the connected BrowserRuntime so ToolExecutor dispatches
        # through the real adapter interface (ref-safe, honest acks).
        # Explicit-attach mode connects the endpoint the operator named;
        # it is never inferred and never the user's browser by default.
        try:
            if self.cdp_endpoint:
                from .browser_owned import OwnedBrowserRuntime
                rt = OwnedBrowserRuntime.attach(self.cdp_endpoint)
                rt.connect()
                self.browser_runtime = rt
                print(f"Browser attached: explicit CDP endpoint "
                      f"{self.cdp_endpoint}")
            else:
                rt = self.browser.runtime
                self.browser_runtime = rt if rt.connected else None
        except Exception as e:
            print(f"Browser connect failed: {e}")
            self.browser_runtime = None
        # Stage E: once a managed browser is connected, drive WebMCP from
        # the live page's model context via CDP. Attached-extension mode
        # keeps the snapshot transport (set in __init__).
        try:
            if getattr(self, "browser_runtime", None) is not None and \
                    getattr(self.browser_runtime, "connected", False):
                from .webmcp.transport import CdpModelContextTransport
                rt = self.browser_runtime
                self.webmcp_gateway.transport = CdpModelContextTransport(
                    rt.evaluate_js)
                print("WebMCP gateway: live model-context transport (CDP)")
        except Exception as e:
            print(f"WebMCP transport wiring failed: {e}")
        self.loop.run_forever()

    # -- bus fan-out -----------------------------------------------------------
    def _on_event(self, ev: dict) -> None:

        t, d = ev["type"], ev["data"]
        if t in ("page.loaded", "page.navigated", "dom.changed", "value.changed",
                 "focus.changed", "human.action", "agent.action", "agent.lease",
                 "agent.goal", "dialog.opened", "dialog.closed"):
            self.world.apply_event({"type": t, "data": d})
            # Stage D: a navigation replaces the tab's document identity so
            # refs pinned against the old document fail closed afterwards.
            if t == "page.navigated" and d.get("url"):
                try:
                    self.sessions.navigate(
                        d.get("tab_id", "default"), d["url"],
                        d.get("title", ""),
                        window_id=d.get("window_id", "win_default"),
                        session_id=d.get("session_id"))
                except Exception:
                    pass
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
        # The unpacked extension's content script pushes snapshots from
        # arbitrary web origins to this loopback harness. Without CORS
        # headers the browser blocks the push (found by live-browser
        # verification; the fake backend never performs a real fetch).
        # The harness binds 127.0.0.1 and is unauthenticated by design;
        # these headers match that local-operator threat model.
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(body)

    def do_OPTIONS(self) -> None:
        # CORS preflight for the extension's cross-origin JSON pushes.
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods",
                         "POST, GET, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.send_header("Content-Length", "0")
        self.end_headers()

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
            from urllib.parse import parse_qs
            q = parse_qs(urlparse(self.path).query)
            sid = (q.get("session_id") or [None])[0]
            return self._send(200, {"summary":
                                    STATE.mem.summary_for_prompt(
                                        session_id=sid),
                                    "prefs": STATE.mem.all_prefs(),
                                    "recent": STATE.mem.recent(
                                        10, session_id=sid)})
        if path == "/audit":
            return self._send(200, {"trail": STATE.safety.audit_trail(),
                                    "chain_valid": STATE.safety.verify_chain()})
        if path == "/world":
            return self._send(200, {"world": STATE.world.snapshot(),
                                    "ownership": STATE.ownership.detailed_snapshot(),
                                    "sessions": STATE.sessions.snapshot(),
                                    "events": STATE.bus.recent(30)})
        if path == "/ladder":
            return self._send(200, {"levels": STATE.ladder.describe()})
        if path == "/workflows":
            return self._send(200, {"workflows": STATE.wfmem.all_workflows()})
        # Stage G: learned workflows (Phase 13) + model registry (Phase 14).
        if path == "/workflows/learned":
            return self._send(200, {"ok": True,
                                    "workflows": STATE.workflow_learner.list()})
        if path == "/models":
            return self._send(200, {"ok": True,
                                    **STATE.model_router.registry.to_dict()})
        # Stage F: origin policy registry (default-deny). Operators must
        # register an origin before the agent may act on it.
        if path == "/policy":
            return self._send(200, {"ok": True,
                                    "origins": STATE.policy.snapshot()})
        # Beta.1: WebMCP GET = list-only, no body (query params allowed)
        if path == "/webmcp/capabilities":
            from urllib.parse import parse_qs
            q = parse_qs(urlparse(self.path).query)
            return self._webmcp_capabilities(
                {k: v[0] for k, v in q.items() if v})
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
        # Stage C: closed-loop agent endpoints (the extension's new runTask).
        if path == "/agent/begin":
            return self._agent_begin(b)
        if path == "/agent/observe":
            return self._agent_observe(b)
        if path == "/agent/next":
            return self._agent_next(b)
        if path == "/agent/report":
            return self._agent_report(b)
        if path == "/agent/status":
            return self._agent_status(b)
        if path == "/event":
            ev = STATE.bus.emit(b.get("type", "unknown"), b.get("data", {}))
            return self._send(200, {"ok": True, "event": ev,
                                    "world_version": STATE.world.version})
        if path == "/lease":
            return self._lease(b)
        if path == "/estop":
            return self._estop(b)
        # Stage G: validating task compiler (Phase 5). Accepts {"spec": {...}}
        # (preferred) or the legacy {"intent", "slots"}. Every action is
        # validated against the capability registry and the origin policy
        # BEFORE the plan is returned; failures are typed CompileErrors.
        if path == "/compile":
            spec = b.get("spec")
            if spec is None:
                spec = {"task": b.get("intent", ""),
                        **(b.get("slots") or {})}
            try:
                plan = compile_spec(spec, policy=STATE.policy,
                                    task_id=b.get("task_id", "compile"))
            except CompileError as e:
                return self._send(400, e.to_dict())
            return self._send(200, {"ok": True, "plan": plan,
                                    "actions": ir_to_actions(plan)})
        if path == "/human":
            STATE.tools.set_human_active(bool(b.get("active", True)))
            return self._send(200, {"paused_for_user": STATE.tools.paused_for_user})
        if path == "/memory/pref":
            STATE.mem.set_pref(b.get("key", ""), b.get("value", ""))
            return self._send(200, {"ok": True})
        if path == "/memory/forget":
            STATE.mem.forget_all()
            return self._send(200, {"ok": True, "forgotten": True})
        # Stage F: verified deletion of one record, or of a whole session.
        if path == "/memory/delete":
            try:
                rid = int(b.get("record_id", -1))
            except (TypeError, ValueError):
                return self._send(400, {"ok": False, "error":
                                        "invalid_record_id"})
            ok = STATE.mem.delete_record(rid, b.get("session_id"))
            return self._send(200, {"ok": ok, "deleted": ok})
        if path == "/memory/forget_session":
            n = STATE.mem.forget_session(b.get("session_id", ""))
            return self._send(200, {"ok": True, "deleted": n})
        # Stage F: origin policy management (operator trust decisions).
        if path == "/policy/origin":
            try:
                pol = STATE.policy.register_origin(
                    b.get("origin", ""),
                    allow=b.get("allow", []), deny=b.get("deny", []),
                    require_consent=b.get("require_consent", []),
                    min_trust=b.get("min_trust", "page-advertised"),
                    description=b.get("description", ""))
            except (ValueError, TypeError) as e:
                # Typed HTTP 400, never a leaked traceback.
                return self._send(400, {"ok": False, "error": "invalid_origin",
                                        "detail": str(e)})
            return self._send(200, {"ok": True, "origin": pol.origin,
                                    "policy_id": pol.policy_id(),
                                    "policy": STATE.policy.snapshot()
                                    [pol.origin]})
        if path == "/policy/origin/remove":
            removed = STATE.policy.remove_origin(b.get("origin", ""))
            return self._send(200, {"ok": removed, "removed": removed})
        if path == "/workflow/observe":
            return self._workflow_observe(b)
        if path == "/workflow/suggest":
            domain = b.get("domain", "")
            prep = STATE.wfmem.prepare(domain, b.get("intent", ""))
            tier, reason = pick_tier(b.get("intent", ""), workflow_matched=prep["matched"])
            return self._send(200, {"preparation": prep, "tier": tier,
                                    "tier_reason": reason})
        # Stage G: learned-workflow family (Phase 13). These workflows are
        # recorded only from fully-verified transactions (see
        # _record_task_report) and replayed through the execution gateway
        # with per-action verification, never blindly.
        if path == "/workflows/learned":
            return self._send(200, {"ok": True,
                                    "workflows": STATE.workflow_learner.list()})
        if path == "/workflows/get":
            w = STATE.workflow_learner.get(b.get("name", ""))
            if w is None:
                return self._send(404, {"ok": False, "error": "not found"})
            return self._send(200, {"ok": True, "workflow": w})
        if path == "/workflows/suggest":
            sugg = STATE.workflow_learner.suggest(
                b.get("goal", ""), top_n=int(b.get("top_n", 3) or 3))
            return self._send(200, {"ok": True, "suggestions": sugg})
        if path == "/workflows/learn":
            rec = STATE.recent_reports.get(b.get("task_id", ""))
            if rec is None:
                return self._send(404, {"ok": False,
                                        "error": "no recorded report for "
                                                 "task_id"})
            learned = STATE.workflow_learner.learn_verified_pairs(
                rec["pairs"], goal=b.get("goal") or rec.get("goal", ""),
                domain=b.get("domain", ""), origin=rec.get("origin", ""),
                session_id=rec.get("session_id"),
                task_id=b.get("task_id"))
            code = 200 if learned.get("learned") else 422
            return self._send(code, learned)
        if path == "/workflows/replay":
            name = b.get("name", "")
            try:
                actions = STATE.workflow_learner.render(
                    name, b.get("params"))
            except KeyError as e:
                return self._send(404, {"ok": False, "error": str(e)})
            result = STATE.gateway.execute({
                "actions": actions, "task_id": f"wf_{name}",
                "tab_id": b.get("tab_id", "default"),
                "session_id": b.get("session_id"),
                "window_id": b.get("window_id", "win_default"),
                "frame_id": b.get("frame_id", "main"),
                "page_url": b.get("page_url", ""),
                "user_consented": bool(b.get("user_consented", False)),
            })
            result["ok"] = (result.get("transaction_status") == "COMMITTED")
            return self._send(200, {"ok": result["ok"], "workflow": name,
                                    "rendered_actions": actions,
                                    "engine_result": result})
        # Stage G: model registry + capability router (Phase 14).
        if path == "/models":
            return self._send(200, {"ok": True,
                                    **STATE.model_router.registry.to_dict()})
        if path == "/route":
            decision = STATE.model_router.route(b.get("requirements") or {},
                                                task_id=b.get("task_id"))
            return self._send(200, dict(decision))
        if path == "/benchmark":
            return self._send(200, _run_benchmarks())
        # Stage G: honest SEQUENTIAL task queue (Phase 9). /task/submit
        # enqueues (compiling "spec" first when present, with typed errors);
        # /task/run dispatches the next ready task through the execution
        # gateway with per-action verification; /task/poll and /task/cancel
        # query and cancel. Tasks run one at a time; a task only starts
        # when its tab lease is free.
        if path == "/task/submit":
            spec = {
                "task_id": b.get("task_id"),
                "goal": b.get("goal") or b.get("intent", ""),
                "tab_id": b.get("tab_id", "default"),
                "session_id": b.get("session_id"),
                "window_id": b.get("window_id", "win_default"),
                "frame_id": b.get("frame_id", "main"),
                "page_url": b.get("page_url", ""),
                "origin": b.get("origin"),
                "user_consented": bool(b.get("user_consented", False)),
                "spec": b.get("spec"),
                "actions": b.get("actions"),
                "depends_on": b.get("depends_on")
                or b.get("dependencies") or [],
                "budgets": b.get("budgets") or {},
                "requirements": b.get("requirements") or {},
            }
            try:
                task = STATE.task_scheduler.submit(
                    spec, policy=STATE.policy, router=STATE.model_router)
            except CompileError as e:
                return self._send(400, e.to_dict())
            except ValueError as e:
                return self._send(409, {"ok": False, "error": str(e)})
            return self._send(200, {"ok": True, **task.to_dict()})
        if path == "/task/poll":
            tid = b.get("task_id", "t1")
            st = STATE.task_scheduler.status(tid)
            code = 200 if st.get("ok") else 404
            return self._send(code, st)
        if path == "/task/cancel":
            tid = b.get("task_id", "")
            ok = STATE.task_scheduler.cancel(tid)
            body = {"ok": ok, "task_id": tid, "cancelled": ok}
            if ok:
                body["task"] = STATE.task_scheduler.status(tid)
            return self._send(200, body)
        if path == "/task/run":
            def _exec(task):
                if STATE.task_scheduler.cancel_requested(task["task_id"]):
                    return {"ok": False, "cancelled": True,
                            "error": "cancel requested"}
                actions = task.get("actions") or []
                if not actions:
                    return {"ok": False,
                            "error": "no_actions: task has no compiled "
                                     "actions; submit with 'spec' or "
                                     "'actions'"}
                result = STATE.gateway.execute({
                    "actions": actions, "task_id": task["task_id"],
                    "tab_id": task["tab_id"],
                    "session_id": task.get("session_id"),
                    "window_id": task.get("window_id", "win_default"),
                    "frame_id": task.get("frame_id", "main"),
                    "page_url": task.get("page_url", ""),
                    "user_consented": task.get("user_consented", False),
                })
                # A task completes only when its whole transaction
                # COMMITTED (every action verified). Partial commits mark
                # the task failed with the partial report attached.
                result["ok"] = (result.get("transaction_status")
                                == "COMMITTED")
                self._record_task_report(task, result)
                return result
            ran = STATE.task_scheduler.run_next(_exec)
            if ran is None:
                return self._send(200, {"ok": True, "ran": None,
                                        "note": "no dispatchable task"})
            return self._send(200, {"ok": True, "ran": ran})
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
        # Beta.1: WebMCP endpoints (Stage E: discover/capabilities/invoke
        # are real; /webmcp/execute is the legacy PARTIAL fixture path)
        if path == "/webmcp/discover":
            return self._webmcp_discover(b)
        if path == "/webmcp/capabilities":
            return self._webmcp_capabilities(b)
        if path == "/webmcp/invoke":
            return self._webmcp_invoke(b)
        if path == "/webmcp/execute":
            return self._webmcp_execute(b)
        return self._send(404, {"ok": False, "error": "not found"})

    # -- handlers ------------------------------------------------------------------
    def _snapshot(self, b: dict):
        tab_id = b.get("tab_id", "default")
        window_id = b.get("window_id", "win_default")
        frame_id = b.get("frame_id", "main")
        session_id = b.get("session_id")  # None -> default session
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
            # Extension push mode (default): body carries url/nodes.
            # Tab identity comes from the extension (background.js pins the
            # tab at task start); it is never inferred from "current tab".
            url = b.get("url", "")
            nodes = b.get("nodes", [])
        # Canonical session registration: every snapshot carries explicit
        # (session, window, tab, frame) identity, including the frame-tree
        # position and Chrome's own frame id when the client knows it.
        sess = STATE.sessions.get_or_create_session(session_id)
        # Stage D: a URL change on the main frame is a navigation -> the
        # document identity is replaced BEFORE the new snapshot is
        # registered, so stale refs fail closed.
        prev_url = STATE.sessions.tab_url(tab_id,
                                          session_id=sess.session_id)
        if frame_id == MAIN_FRAME and url and prev_url and url != prev_url:
            STATE.sessions.navigate(tab_id, url, title, window_id=window_id,
                                    session_id=sess.session_id)
        STATE.sessions.register_tab(tab_id, window_id, sess.session_id,
                                    url=url, title=title)
        STATE.sessions.register_frame(tab_id, frame_id, window_id,
                                      sess.session_id, url=url,
                                      parent_frame_id=b.get("parent_frame_id"),
                                      frame_chain=b.get("frame_chain"),
                                      name=b.get("frame_name", ""),
                                      chromium_frame_id=b.get(
                                          "chromium_frame_id"))
        for i, n in enumerate(nodes):
            n.setdefault("index", i)
            n.setdefault("frame_id", frame_id)
        flat = json.dumps(nodes)[:20000]
        # Stage F: page text is untrusted data. Deterministic injection
        # scan; hits are quarantined (flagged + journaled), never treated
        # as instructions.
        from .contamination import classify_text, quarantine_id
        inj = classify_text(flat)
        injected = inj["injection"]
        if injected:
            qid = quarantine_id(sess.session_id, tab_id, frame_id,
                                inj["markers"][0] if inj["markers"] else "")
            STATE.bus.emit("security.quarantine",
                           {"quarantine_id": qid, "kind": "page_text",
                            "session_id": sess.session_id, "tab_id": tab_id,
                            "frame_id": frame_id, "url": url,
                            "markers": inj["markers"]})
            STATE.safety.log({"tool": "snapshot", "url": url},
                             f"quarantined:page_text:{qid}")
        view = STATE.ctx.ingest(url, nodes, b.get("goal", ""),
                                b.get("screenshot_note", ""),
                                session_id=sess.session_id, tab_id=tab_id,
                                frame_id=frame_id)
        obs = STATE.sessions.record_observation(tab_id, frame_id,
                                                snapshot_hash=view["hash"],
                                                window_id=window_id,
                                                session_id=sess.session_id)
        # Stage E: cache page-reported model-context tools under the scope
        # key (session, tab, frame, document). The document id is the
        # generation key: a navigation changes it, so a stale entry can
        # never be returned for the new document. A page that reports no
        # model context is cached as unavailable (never popped) so the
        # gateway fails closed instead of falling through to a fixture.
        webmcp = b.get("webmcp")
        if isinstance(webmcp, dict):
            from .webmcp.scope import WebMCPScope
            doc_id = STATE.sessions.frame_document_id(
                tab_id, frame_id, session_id=sess.session_id)
            # The cache key IS the scope key: a navigation changes the
            # document id, so a stale entry can never be returned for the
            # new document.
            key = WebMCPScope(session_id=sess.session_id, tab_id=tab_id,
                              frame_id=frame_id,
                              document_id=doc_id).key()
            tools = webmcp.get("tools")
            # Carry the page-REPORTED availability explicitly: a model
            # context with zero tools is available, not unavailable.
            page_available = bool(webmcp.get("available"))
            if page_available and isinstance(tools, list):
                STATE.webmcp_snapshots[key] = {
                    "available": True, "tools": tools, "reason": ""}
            else:
                STATE.webmcp_snapshots[key] = {
                    "available": False, "tools": [],
                    "reason": (webmcp.get("unavailable_reason") or
                               "webmcp_unavailable")}
        # Stage C dedup: the snapshot used to be journaled twice -- once by the
        # direct world.load_full() call below and once via the page.loaded
        # bus event. Now there is exactly one ingest path: the bus event
        # carries the full snapshot payload and _on_event applies it once.
        STATE.bus.emit("page.loaded", {"url": url, "title": title,
                                       "nodes": view["nodes"],
                                       "full": True,
                                       "tab_id": tab_id, "window_id": window_id,
                                       "frame_id": frame_id,
                                       "session_id": sess.session_id,
                                       "observation_id": obs["observation_id"],
                                       "task_id": b.get("task_id"),
                                       "action_id": b.get("action_id")})
        view["injection_suspected"] = injected
        view["world_version"] = STATE.world.version
        view["world"] = STATE.world.prompt_section()
        view["session_id"] = sess.session_id
        view["window_id"] = window_id
        view["frame_id"] = frame_id
        view["observation_id"] = obs["observation_id"]
        view["prompt"] = STATE.ctx.prompt_for_tab(b.get("goal", ""), tab_id,
                                                  STATE.mem.summary_for_prompt())
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
        state_sig = ("v%d:" % STATE.world.version) + hashlib.sha256(
            json.dumps(page_state, sort_keys=True, default=str).encode()
        ).hexdigest()[:16]
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
        prompt = STATE.ctx.prompt_for_tab(goal, STATE.world.active_tab,
                                          STATE.mem.summary_for_prompt())
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
        """Single-action / action-batch execution.

        Thin adapter over ExecutionGateway.execute(): the gateway runs the
        honest lifecycle (REQUEST -> VALIDATE -> RESERVE -> PRECONDITION
        CHECK -> DISPATCH -> ACK -> OBSERVE -> VERIFY -> COMMIT), records
        per-action claims with the global verifier, and returns the
        aggregate transaction status. This endpoint only shapes the
        response; it adds no execution semantics of its own.
        """
        out = STATE.gateway.execute({
            "actions": b.get("actions") or ([b["action"]] if "action" in b else []),
            "task_id": b.get("task_id"),
            "tab_id": b.get("tab_id", "default"),
            "session_id": b.get("session_id"),
            "window_id": b.get("window_id", "win_default"),
            "frame_id": b.get("frame_id", "main"),
            "page_url": b.get("page_url", ""),
            "user_consented": bool(b.get("user_consented", False)),
            "note": b.get("note", ""),
        })
        # Compatibility keys: the first action's verification + claim id,
        # and the legacy per-action "results" list (execution outcomes).
        verifications = out["verifications"]
        results = [{"ok": e["state"] not in ("FAILED", "CONFLICTING", "CANCELLED"),
                    "tool": e["tool"], "state": e["state"],
                    "error_code": e["error_code"], "error": e["error"]}
                   for e in out["action_results"]]
        return self._send(200, {"results": results,
                                "task_id": out["task_id"],
                                "transaction_id": out["transaction_id"],
                                "transaction_status": out["transaction_status"],
                                "action_results": out["action_results"],
                                "verifications": verifications,
                                "claim_id": (verifications[0]["claim_id"]
                                             if verifications else None),
                                "verification": verifications[0]
                                if verifications else None,
                                "latency_ms": out["latency_ms"],
                                "paused_for_user": STATE.tools.paused_for_user})

    def _transact(self, b: dict):
        """Transactional co-execution via the execution gateway.

        Previously ran its own ad-hoc lease/execute/verify sequence with a
        single batch claim; now every action gets its own lifecycle,
        exclusive lease, and per-action claim. Returns the aggregate
        transaction status (COMMITTED / PARTIALLY_COMMITTED / FAILED /
        UNVERIFIED).
        """
        out = STATE.gateway.execute({
            "actions": b.get("actions") or ([b["action"]] if "action" in b else []),
            "task_id": b.get("task_id"),
            "transaction_id": b.get("transaction_id"),
            "tab_id": b.get("tab_id", "default"),
            "session_id": b.get("session_id"),
            "window_id": b.get("window_id", "win_default"),
            "frame_id": b.get("frame_id", "main"),
            "page_url": b.get("page_url", ""),
            "user_consented": bool(b.get("user_consented", False)),
            "note": b.get("note", "tx"),
        })
        verifications = out["verifications"]
        results = [{"ok": e["state"] not in ("FAILED", "CONFLICTING", "CANCELLED"),
                    "tool": e["tool"], "state": e["state"],
                    "verdict": ("executed" if e["state"] == "VERIFIED"
                                else "replan"),
                    "error_code": e["error_code"], "error": e["error"]}
                   for e in out["action_results"]]
        conflicts = sum(1 for r in results
                        if r["state"] in ("CONFLICTING", "CANCELLED"))
        return self._send(200, {"results": results, "conflicts": conflicts,
                                "task_id": out["task_id"],
                                "transaction_id": out["transaction_id"],
                                "transaction_status": out["transaction_status"],
                                "action_results": out["action_results"],
                                "verifications": verifications,
                                "claim_id": (verifications[0]["claim_id"]
                                             if verifications else None),
                                "verification": verifications[0]
                                if verifications else None,
                                "latency_ms": out["latency_ms"],
                                "ownership": STATE.ownership.snapshot()})

    def _agent_begin(self, b: dict):
        """Start a task-scoped closed loop: mint task_id, pin tab identity."""
        goal = b.get("goal", "")
        identity = b.get("identity") or {}
        ctx = STATE.agent.begin(goal, identity,
                                max_steps=int(b.get("max_steps", 15)))
        return self._send(200, {"ok": True, "task_id": ctx.task_id,
                                "goal": goal,
                                "tab_id": ctx.tab_id,
                                "window_id": ctx.window_id,
                                "frame_id": ctx.frame_id,
                                "max_steps": ctx.max_steps})

    def _agent_view(self, tab_id: str) -> dict:
        obs = STATE.world.tab_observation(tab_id) or {}
        return {"url": obs.get("url", ""), "observation_id":
                obs.get("observation_id"),
                "snapshot_version": obs.get("observation_version")}

    def _agent_observe(self, b: dict):
        """OBSERVE: ingest the latest world state for the task's pinned tab.

        The fresh snapshot itself arrives via /snapshot (extension push or
        CDP pull); this call only compares the task's pinned context against
        current world state and returns change flags the client uses on the
        next step.
        """
        ctx = STATE.agent.get(b.get("task_id", ""))
        if ctx is None:
            return self._send(404, {"ok": False, "error": "unknown task_id"})
        flags = STATE.agent.observe(ctx, self._agent_view(ctx.tab_id))
        return self._send(200, {"ok": True, "task_id": ctx.task_id,
                                "flags": flags,
                                "steps_used": ctx.steps_used,
                                "steps_remaining": ctx.max_steps - ctx.steps_used,
                                "status": ctx.status})

    def _agent_next(self, b: dict):
        """OBSERVE + SELECT CAPABILITY + PLAN + VALIDATE + LEASE.

        Returns {"decision": "act", "action": {...lease held...}} or a
        terminal decision (success | continue | replan | request-human |
        abort). The caller executes at most ONE mutation for "act", then
        pushes the post-action snapshot via /snapshot and calls /agent/report.
        """
        ctx = STATE.agent.get(b.get("task_id", ""))
        if ctx is None:
            return self._send(404, {"ok": False, "error": "unknown task_id"})
        flags = STATE.agent.observe(ctx, self._agent_view(ctx.tab_id))
        decision = STATE.agent.next_action(ctx, flags)
        return self._send(200, {"ok": True, "task_id": ctx.task_id,
                                "flags": flags, **decision})

    def _agent_report(self, b: dict):
        """VERIFY + DECIDE. status in {executed, not_executed, browser_failed}.

        The client MUST push the post-action observation via /snapshot
        before calling this with status="executed"; the loop records that
        fresh canonical-state read as evidence and re-verifies the claim.
        The client's browser_ack (Stage D) is validated and recorded as
        BROWSER_ACK evidence (audit trail only) before verification.
        """
        ctx = STATE.agent.get(b.get("task_id", ""))
        if ctx is None:
            return self._send(404, {"ok": False, "error": "unknown task_id"})
        decision = STATE.agent.report(
            ctx, b.get("action_id"), b.get("claim_id"),
            b.get("status", "executed"),
            error_code=b.get("error_code"), reason=b.get("reason"),
            browser_ack=b.get("browser_ack"),
            webmcp_result=b.get("webmcp_result"))
        return self._send(200, {"ok": True, "task_id": ctx.task_id,
                                "verified": ctx.verified,
                                "replans": ctx.replans,
                                "steps_used": ctx.steps_used,
                                "status": ctx.status, **decision})

    def _agent_status(self, b: dict):
        ctx = STATE.agent.get(b.get("task_id", ""))
        if ctx is None:
            return self._send(404, {"ok": False, "error": "unknown task_id"})
        return self._send(200, {"ok": True, "task_id": ctx.task_id,
                                "goal": ctx.goal, "tab_id": ctx.tab_id,
                                "status": ctx.status,
                                "steps_used": ctx.steps_used,
                                "steps_remaining": ctx.max_steps - ctx.steps_used,
                                "verified": ctx.verified,
                                "replans": ctx.replans,
                                "consecutive_failures":
                                    ctx.consecutive_failures,
                                "history": ctx.history[-20:]})

    def _lease(self, b: dict):
        if b.get("release"):
            # Compare-and-release: prefer the explicit (target, lease_id)
            # form; falls back to lease-id lookup for backwards compat.
            if b.get("target"):
                ok = STATE.leases.release(b["target"], b["release"])
            else:
                ok = STATE.leases.release(b["release"])
            return self._send(200, {"ok": ok, "released": b["release"]})
        hierarchy = ()
        if b.get("tab_id"):
            from .concurrency import hierarchy_for
            hierarchy = hierarchy_for(b.get("tab_id"), b.get("frame_id"),
                                      b.get("session_id"))
        lease = STATE.leases.acquire(b.get("target", ""), b.get("intent", ""),
                                     ttl=float(b.get("ttl", 2.0)),
                                     task_id=b.get("task_id"),
                                     owner_hierarchy=hierarchy)
        if lease is None:
            if STATE.leases.stopped:
                return self._send(409, {"ok": False,
                                        "error": "emergency stop active"})
            return self._send(409, {"ok": False,
                                    "error": "target held by another lease, "
                                             "human-owned, or conflicted; "
                                             "replan or request ownership"})
        STATE.bus.emit("agent.lease", {"lease": lease["lease"], "target": lease["target"],
                                       "task_id": b.get("task_id"),
                                       "tab_id": b.get("tab_id", "default")})
        return self._send(200, {"ok": True, **lease})

    def _estop(self, b: dict):
        """Global emergency stop: revoke all agent leases, block new ones."""
        if b.get("active", True):
            revoked = STATE.leases.emergency_stop()
            STATE.bus.emit("agent.estop", {"active": True, "revoked": revoked})
            return self._send(200, {"ok": True, "emergency_stop": True,
                                    "leases_revoked": revoked})
        STATE.leases.clear_emergency()
        STATE.bus.emit("agent.estop", {"active": False})
        return self._send(200, {"ok": True, "emergency_stop": False})

    # Stage G: keep a bounded store of gateway reports per task so
    # /workflows/learn can learn from a verified transaction, and
    # auto-learn workflows from fully-COMMITTED task transactions.
    # COMMITTED means every action independently verified (see
    # TransactionEngine._aggregate), so only genuinely verified work is
    # ever learned.
    def _record_task_report(self, task: dict, result: dict) -> None:
        actions = task.get("actions") or []
        action_results = result.get("action_results") or []
        pairs = [(a, (ar.get("verification") or {}))
                 for a, ar in zip(actions, action_results)]
        STATE.recent_reports[task["task_id"]] = {
            "task_id": task["task_id"], "goal": task.get("goal", ""),
            "origin": task.get("origin", ""), "domain": task.get("origin", ""),
            "session_id": task.get("session_id"), "pairs": pairs,
            "transaction_status": result.get("transaction_status"),
        }
        while len(STATE.recent_reports) > 50:
            oldest = next(iter(STATE.recent_reports))
            STATE.recent_reports.pop(oldest, None)
        if result.get("transaction_status") == "COMMITTED" and pairs:
            try:
                STATE.workflow_learner.learn_verified_pairs(
                    pairs, goal=task.get("goal", ""),
                    domain=task.get("origin", ""),
                    origin=task.get("origin", ""),
                    session_id=task.get("session_id"),
                    task_id=task["task_id"])
            except Exception:
                pass  # learning never breaks execution

    def _workflow_observe(self, b: dict):
        steps = b.get("steps", [])
        if steps:
            STATE.miner.observe(steps)
        confirmed = []
        for c in STATE.miner.candidates():
            if c["observed_runs"] >= 3 or (c["observed_runs"] >= 2 and b.get("confirm")):
                from .session import stable_id as _stable_id
                name = f"{c['domain'] or 'web'}/{_stable_id('wf', *(c['steps'] or []))[-4:]}"
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

    # Stage E: WebMCP endpoints (real model-context discovery per
    # session/tab/frame, transaction-engine invocation, deterministic
    # selection). /webmcp/execute below is the legacy Beta.1 fixture
    # path, kept for compatibility and labeled PARTIAL.
    def _webmcp_scope_from(self, b: dict):
        scope = STATE.webmcp_gateway.scope_for(
            b.get("session_id"), b.get("tab_id", "default"),
            b.get("frame_id", "main"))
        if scope is None:
            raise ValueError("WEBMCP_SCOPE_VIOLATION: unknown tab/session "
                             "(push a snapshot first)")
        return scope

    def _webmcp_discover(self, b: dict):
        try:
            scope = self._webmcp_scope_from(b)
        except ValueError as e:
            return self._send(400, {"ok": False, "error": str(e),
                                    "error_code": "WEBMCP_SCOPE_VIOLATION"})
        result = STATE.webmcp_gateway.discover(scope)
        return self._send(200, {"ok": True,
                                "available": result.get("available", False),
                                "tools": result.get("tools", []),
                                "document_id": scope.document_id,
                                "scope": {"session_id": scope.session_id,
                                          "tab_id": scope.tab_id,
                                          "frame_id": scope.frame_id},
                                "unavailable_reason": result.get("reason", ""),
                                "fallback": {"enabled": False}})

    def _webmcp_capabilities(self, b: dict):
        """Deterministic capability selection for a goal (Stage E).

        Returns the FULL selection record: every considered tool, its
        score breakdown, the winner, and the rationale -- written to the
        audit trail via the gateway bus.
        """
        goal = b.get("goal", "")
        try:
            scope = self._webmcp_scope_from(b)
        except ValueError as e:
            return self._send(400, {"ok": False, "error": str(e),
                                    "error_code": "WEBMCP_SCOPE_VIOLATION"})
        result = STATE.webmcp_gateway.discover(scope)
        record = STATE.webmcp_gateway.select(goal, scope)
        return self._send(200, {"ok": True,
                                "available": result.get("available", False),
                                "goal": goal,
                                "selection": record.to_dict()})

    def _webmcp_invoke(self, b: dict):
        """Invoke a page-advertised WebMCP tool (Stage E).

        Routes through the transaction engine exactly like /act: the same
        identity guards, lease reservation, claim proposal, evidence
        recording, and per-action verification. ``goal`` without
        ``tool_name`` triggers deterministic selection first (its full
        rationale is echoed in the response).
        """
        tool_name = b.get("tool_name", "")
        goal = b.get("goal", "")
        selection = None
        if not tool_name:
            if not goal:
                return self._send(400, {"ok": False,
                                        "error": "tool_name or goal required"})
            try:
                scope = self._webmcp_scope_from(b)
            except ValueError as e:
                return self._send(400, {"ok": False, "error": str(e),
                                        "error_code": "WEBMCP_SCOPE_VIOLATION"})
            result = STATE.webmcp_gateway.discover(scope)
            selection = STATE.webmcp_gateway.select(goal, scope)
            if not selection.winner:
                return self._send(404, {"ok": False,
                                        "error": "no tool matched the goal",
                                        "selection": selection.to_dict()})
            tool_name = selection.winner
        action = {"tool": "webmcp_invoke", "tool_name": tool_name,
                  "args": b.get("args", {}), "intent": goal or tool_name}
        out = STATE.gateway.execute({
            "actions": [action],
            "task_id": b.get("task_id"),
            "tab_id": b.get("tab_id", "default"),
            "session_id": b.get("session_id"),
            "window_id": b.get("window_id", "win_default"),
            "frame_id": b.get("frame_id", "main"),
            "page_url": b.get("page_url", ""),
            "user_consented": bool(b.get("user_consented", False)),
            "note": b.get("note", ""),
        })
        verifications = out["verifications"]
        resp = {"ok": out["transaction_status"] == "COMMITTED",
                "tool_name": tool_name,
                "task_id": out["task_id"],
                "transaction_id": out["transaction_id"],
                "transaction_status": out["transaction_status"],
                "action_results": out["action_results"],
                "verifications": verifications,
                "verification": verifications[0] if verifications else None,
                "latency_ms": out["latency_ms"]}
        if selection is not None:
            resp["selection"] = selection.to_dict()
        return self._send(200, resp)

    def _webmcp_execute(self, b: dict):
        # LEGACY Beta.1 path (pre-Stage E): the old adapter answers from
        # its built-in fixture table without touching any page. Kept for
        # compatibility; labeled PARTIAL and never verification-capable.
        origin = b.get("origin", "")
        tool = b.get("tool", "")
        args = b.get("args", {})
        goal = b.get("goal", "")
        user_consented = bool(b.get("user_consented", False))
        if not origin or not tool:
            return self._send(400, {"ok": False, "error": "origin and tool required"})
        res = STATE.webmcp_adapter.execute(origin, tool, args, goal, user_consented)
        return self._send(200, {"ok": res.ok, "result": res.result,
                                "error": res.error,
                                "mode": "PARTIAL",
                                "caveat": ("legacy fixture result: did not "
                                           "come from a page; cannot verify "
                                           "any live-page claim")})


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
        # Stage F: the synthetic benchmark origin is explicitly trusted so
        # the run exercises conflict avoidance, not policy default-deny.
        tx.engine.policy.register_origin(
            "bench.shop", allow=["read", "navigate", "interact"],
            description="synthetic benchmark origin")
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
                    help="Managed launch: start a SYNK-owned Chromium with a "
                         "persistent SYNK profile (requires playwright). "
                         "NEVER attaches to the user's browser. "
                         "Default: attached-extension mode.")
    ap.add_argument("--cdp-endpoint", default="",
                    help="Explicit CDP endpoint to attach to "
                         "(ws://host:port/devtools/browser/<id>). Mutually "
                         "exclusive with --use-cdp; attach mode only talks to "
                         "the endpoint you name.")
    ap.add_argument("--profile-dir", default="",
                    help="Profile directory for managed launch (default: "
                         "~/.synk/chromium-profile).")
    ap.add_argument("--webmcp-fallback", action="store_true",
                    help="Opt in to the explicit PARTIAL WebMCP fixture "
                         "fallback when the page has no model context. "
                         "Fallback results are labeled as fixtures and can "
                         "never verify a live-page claim. Default: off "
                         "(fail closed).")
    args = ap.parse_args()
    global STATE
    STATE = State(args.db, use_cdp=args.use_cdp,
                  cdp_endpoint=args.cdp_endpoint,
                  profile_dir=args.profile_dir,
                  webmcp_fallback=args.webmcp_fallback)
    STATE._human_steps = []
    srv = HTTPServer(("127.0.0.1", args.port), Handler)
    print(f"ai-cowork harness on http://127.0.0.1:{args.port} (db={args.db} "
          f"use_cdp={args.use_cdp} cdp_endpoint={'set' if args.cdp_endpoint else ''})")
    srv.serve_forever()


if __name__ == "__main__":
    main()
