# SYNK — Human–Agent Co-Work Browser Runtime

Implements **E:\NEW PROJECT.md** (Alpha → Beta.3 → Phase 2):
a **collaborative browser runtime** where humans and AI agents operate concurrently
on the same web session with structured world state, action ownership, workflow
learning, **WebMCP semantic fast path**, an evidence-backed **Truth Layer**,
and a native **Tauri desktop shell**.

## Architecture (maps to chatgpt spec §1–25 + Beta.1 WebMCP)

```
User <-> extension/sidebar <---> harness/server (127.0.0.1:18080) <---> LLM
            | content.js (AX capture, DOM execution, typed events)   |-> cloud (OPENAI_API_KEY)
            | background.js (bridge, run loop, transact)              |   or local mock
            +-> WorldState + EventBus (event-sourced browser state)
                Concurrency: OwnershipGraph + Leases + ConflictDetector
                TransactionRunner: READ->PLAN->RESERVE->VALIDATE->EXECUTE->VERIFY
                ActionCompiler: LLM intent -> Browser IR -> tool actions
                WorkflowMiner + WorkflowMemory (prefs/facts/workflows/macros/failures)
                ExecutionLadder: L0 WebMCP -> L1 DOM semantic -> L2 primitives -> L3 vision -> L4 human
                ModelRouter: rule -> classifier -> small-local -> cloud -> vision
                WebMCP: Discovery -> Capability Registry -> Policy/Verifier -> Direct Exec
```

## Quickstart

```powershell
# 1. start harness (extension mode; no browser dependency)
python -m harness.server --port 18080 --db agent_memory.db
#    CDP mode (requires: pip install playwright; playwright install chromium):
#    python -m harness.server --port 18080 --use-cdp
#    Real local SLM (see docs/local-models.md):
#    $env:SYNK_LOCAL_MODEL='endpoint'
#    $env:SYNK_LOCAL_MODEL_URL='http://127.0.0.1:8090/v1/chat/completions'
# 2. run unit tests
python -m unittest discover -s tests -v
# 3. run headless demo (terminal 2, harness running)
python demo/demo_script.py
# 4a. load extension in Chrome: chrome://extensions -> Developer mode ->
#     Load unpacked -> select extension/
# 4b. desktop shell (requires Rust + Node): cd shell; npm install; npm run tauri dev
```

With `OPENAI_API_KEY` set, `/plan` uses a cloud model; otherwise (and for
simple queries like "what is the label…") it uses the local deterministic
planner — the prototype works fully offline.

## Alpha endpoints (unchanged)
| Method | Endpoint | Purpose |
|---|---|---|
| GET | `/health` | liveness + version |
| POST | `/snapshot` | ingest page nodes → trimmed context + prompt (carries explicit `tab_id`/`window_id`/`frame_id`/`session_id`) |
| POST | `/plan` | LLM action plan for goal (includes tier, execution level) |
| POST | `/act` | validate+queue actions (bulk supported) |
| POST | `/human` | `{active}` human-priority pause flag |
| GET | `/memory` | summary/prefs/recent |
| POST | `/memory/pref`, `/memory/forget` | learn / GDPR forget |
| GET | `/audit` | tamper-evident log + chain check |

## Beta endpoints (new)
| Method | Endpoint | Purpose |
|---|---|---|
| POST | `/event` | push typed browser event (DOM, focus, human action…) |
| GET | `/world` | WorldState snapshot + ownership + recent events |
| POST | `/lease` | `{target,intent,ttl,tab_id}` → EXCLUSIVE short-lived agent lease (or 409); release is compare-and-release `{target, release: lease_id}` |
| POST | `/estop` | `{active}` global emergency stop: revokes all agent leases, blocks new ones (separate from resource ownership) |
| POST | `/transact` | transactional co-execution (lease+validate+execute+verify) |
| POST | `/compile` | `{intent,slots}` → Browser IR + lowered tool actions |
| GET | `/ladder` | execution ladder levels + registered site adapters |
| POST | `/workflow/observe` | `{steps,intent}` → mine candidates / confirm workflows |
| POST | `/workflow/suggest` | `{domain,intent}` → predictive prep + model tier hint |
| GET | `/workflows` | list confirmed workflows |
| POST | `/benchmark` | run headless Beta benchmark suite (AES) |

## Beta.1 WebMCP endpoints (new)
| Method | Endpoint | Purpose |
|---|---|---|
| POST | `/webmcp/discover` | `{origin|url}` → discover site's WebMCP tools |
| POST | `/webmcp/capabilities` | `{origin?,goal?}` → list capabilities with risk/latency |
| POST | `/webmcp/execute` | `{origin,tool,args,goal?,user_consented?}` → policy-gated execution |

## Key advances

### Alpha (✓)
- Extension scaffold, basic LLM+tools, sidebar, harness loop
- Safety: allowlist, consent, human-priority, PII masking, injection detection, audit chain

### Beta (✓)
1. **WorldState + Event Bus** — event-sourced browser state; LLM receives "WORLD_STATE vN + changed keys"
2. **Concurrency** — ownership graph (FREE/HUMAN/AGENT/CONFLICT), short leases, intent-level conflict detection, transactional execution
3. **Action Compiler** — LLM intent → Browser IR (preconditions + ops + verification) → deterministic tool actions
4. **Execution Ladder** — WebMCP → DOM semantic → primitives → vision → human (graceful degradation)
5. **Workflow Mining** — human actions → pattern detector → confirmed workflows → predictive prep without LLM
6. **Model Router** — 5 tiers (rule → classifier → small-local → cloud → vision)
7. **Benchmark Harness** — AES = task_success / (tokens + latency_w + actions_w)

### Beta.1 (✓)
8. **WebMCP Adapter** — Discovery → Capability Registry → Policy Engine → Verifier → Direct Exec
   - Capabilities have risk classification (low/medium/high/critical), latency class, consent requirements
   - Policy engine enforces ownership/conflict/consent checks on top of site-exposed tools
   - Even `deleteAccount()` from a site goes through the harness safety pipeline
9. **Capability Registry** — unified view of all capabilities across sources (webmcp/dom/primitive/vision)
10. **Benchmarks with Co-working Metrics** — interference rate, agent overlap rate, recovered conflict rate, agent tax

## Safety (Alpha + Beta + Beta.1 §21)

- Fixed tool allowlist; unknown tools ignored. No arbitrary code execution.
- Destructive actions require sidebar consent checkbox.
- Human input pauses the agent; user always wins conflicts (Alpha fallback).
- PII masked in prompts; page text tagged `<untrusted_page_content>`, injection flagged.
- Every decision hash-chained in `/audit`; domain allowlist + banking view-only policy.
- **Beta adds**: ownership + leases + precondition validation before execute.
- **Beta.1 adds**: WebMCP capabilities pass through PolicyEngine (ownership/conflict/consent) before execution.

## Performance (Alpha + Beta + Beta.1)

- Single-snapshot retention, rule-based trim, token estimate per step.
- Bulk fill: whole form in 1 LLM call (demo shows round-trips saved).
- Incremental diffs; model routing (simple→local, complex→cloud).
- **Beta adds**: WorldState diffs replace full snapshots per step; workflow reuse avoids LLM entirely for repeated tasks.
- **Beta.1 adds**: WebMCP Level 0 bypasses DOM interaction entirely for supported sites.

## Status checklist

- [x] Alpha: extension scaffold, basic LLM+tools, sidebar, harness loop
- [x] WorldState + EventBus + incremental diffs
- [x] Ownership + Leases + ConflictDetector + TransactionRunner
- [x] ActionCompiler (Browser IR) + ExecutionLadder + SiteRegistry
- [x] WorkflowMiner + WorkflowMemory (prefs/facts/workflows/macros/failures)
- [x] ModelRouter (5 tiers)
- [x] Benchmark harness (AES) + headless suite + co-working scenarios
- [x] **WebMCP adapter registry (Level 0)**
- [x] **Capability Registry with risk/ownership/policy**
- [x] **WebMCP policy-gated execution + verifier**
- [x] **Truth Layer wired**: `/act`/`/transact` record Evidence + Claims, verify honestly
- [x] **Scheduler execution loop**: `/task/submit|poll|run` with deps + tab guards
- [x] **Real consent flow**: `consent_required` → shell confirm → resubmit consented
- [x] **Tauri shell compiles** (`cargo check` clean): world/ownership/events UI + consent
- [x] **EVE validation**: experience run caught + fixed demo defects (29→33, overlaps gone)
- [x] **Live test green**: snapshot→plan→act→verify, scheduler chain, consent, audit
- [ ] On-device SLM integration (mlc-llm/WebLLM for Tier 1/2; mock stands in)
- [ ] Deep Chromium integration (Phase 3+ of chatgpt roadmap)

## Roadmap

| Phase | Focus | Target |
|---|---|---|
| 1 (now) | Extension-based runtime | ✓ Beta.1 |
| 2 | Desktop shell (Tauri/Electron) | next |
| 3 | Controlled Chromium build | later |
| 4 | Deep browser integration | later |

**Your Alpha does not need to be thrown away** — its `context_manager`, `tools`, `memory`,
`safety`, `orchestrator`, and MV3 bridge are good seeds refactored around
`WorldState + EventBus + Ownership + Transaction + Capabilities`.

## Running the Beta benchmark

```powershell
python -m harness.server --port 18080   # terminal 1
# terminal 2:
python -c "
import http.client, json
conn = http.client.HTTPConnection('127.0.0.1', 18080)
conn.request('POST', '/benchmark', '{}', {'Content-Type': 'application/json'})
print(json.dumps(json.loads(conn.getresponse().read().decode()), indent=2))
"
```

Output includes per-task AES and summary with `mean_aes`, `total_tokens`,
`total_conflicts`, `total_cost_usd`, plus co-working metrics:
`interference_rate`, `agent_overlap_rate`, `recovered_conflict_rate`, `agent_tax`.

## WebMCP quick test

```powershell
python -m harness.server --port 18080   # terminal 1
# terminal 2:
python -c "
import http.client, json
conn = http.client.HTTPConnection('127.0.0.1', 18080)
# Discover tools
conn.request('POST', '/webmcp/discover', json.dumps({'origin': 'shop.example.com'}), {'Content-Type': 'application/json'})
print(json.dumps(json.loads(conn.getresponse().read().decode()), indent=2))
# Execute read-only tool
conn.request('POST', '/webmcp/execute', json.dumps({'origin': 'shop.example.com', 'tool': 'searchProducts', 'args': {'query': 'MacBook'}}), {'Content-Type': 'application/json'})
print(json.dumps(json.loads(conn.getresponse().read().decode()), indent=2))
# Execute destructive tool (needs consent)
conn.request('POST', '/webmcp/execute', json.dumps({'origin': 'shop.example.com', 'tool': 'checkout', 'args': {}, 'user_consented': True}), {'Content-Type': 'application/json'})
print(json.dumps(json.loads(conn.getresponse().read().decode()), indent=2))
"
```