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
# 1. start harness (attached-extension mode; no browser dependency)
python -m harness.server --port 18080 --db agent_memory.db
#    Managed-launch mode: SYNK-owned Chromium, persistent SYNK profile
#    (requires: pip install playwright; playwright install chromium).
#    NEVER the user's browser.
#    python -m harness.server --port 18080 --use-cdp
#    Explicit CDP attach (operator-named endpoint only):
#    python -m harness.server --port 18080 --cdp-endpoint ws://127.0.0.1:9222/devtools/browser/<id>
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
| POST | `/act` | canonical execution via ExecutionGateway: honest per-action lifecycle (REQUEST → VALIDATED → LEASED → PRECONDITION_CHECK → DISPATCHED → ACKNOWLEDGED → OBSERVING → VERIFIED), per-action claims, aggregate `transaction_status` |
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
| POST | `/transact` | transactional co-execution via ExecutionGateway: bulk actions expand to one lifecycle + claim per sub-action; returns `transaction_status` (COMMITTED / PARTIALLY_COMMITTED / FAILED / UNVERIFIED) |
| POST | `/compile` | `{intent,slots}` → Browser IR + lowered tool actions |
| GET | `/ladder` | execution ladder levels + registered site adapters |
| POST | `/workflow/observe` | `{steps,intent}` → mine candidates / confirm workflows |
| POST | `/workflow/suggest` | `{domain,intent}` → predictive prep + model tier hint |
| GET | `/workflows` | list confirmed workflows |
| POST | `/benchmark` | run headless Beta benchmark suite (AES) |

## Beta.1 WebMCP endpoints (new)
| Method | Endpoint | Purpose |
|---|---|---|
| POST | `/webmcp/discover` | `{tab_id, frame_id?, session_id?}` → per-document model-context discovery (Stage E: page-advertised tools only, fail-closed) |
| POST | `/webmcp/capabilities` | `{goal, tab_id, ...}` → deterministic selection: all candidates, score breakdowns, winner, rationale |
| POST | `/webmcp/invoke` | `{tool_name\|goal, args, tab_id, ...}` → invocation through the transaction engine: claims, evidence, verification |
| POST | `/webmcp/execute` | legacy Beta.1 fixture path, kept for compatibility and labeled PARTIAL (never verifies a claim) |

## Stage C endpoints (new): closed-loop agent
| Method | Endpoint | Purpose |
|---|---|---|
| POST | `/agent/begin` | `{goal, identity, max_steps}` → mint `task_id`, pin tab identity |
| POST | `/agent/next` | OBSERVE → plan → VALIDATE → LEASE → returns `{"decision": "act", action, claim_id, lease_id}` or `success / continue / replan / request-human / abort` |
| POST | `/agent/report` | `{task_id, action_id, claim_id, status}` → VERIFY against the post-action observation → DECIDE (lease released exactly once here) |
| POST | `/agent/status` | task state: steps used/remaining, verified count, replans, history |
| POST | `/agent/observe` | OBSERVE only: change flags (navigation, modal, human interference) without planning |

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

### Stage C (✓): honest transactions + closed loop
11. **ExecutionGateway** — the single `ExecutionGateway.execute(request)` used by `/act`, `/transact`, the extension, Tauri (via `/act`), and tests. HTTP endpoints are thin adapters; no caller bypasses the transaction / ownership / verification pipeline.
12. **Honest transaction lifecycle** — every action moves through REQUEST → VALIDATED → LEASED → PRECONDITION_CHECK → DISPATCHED → ACKNOWLEDGED → OBSERVING → VERIFIED. "ACKNOWLEDGED" means the executor accepted the command, never that the browser performed it. Extension mode queues commands; only an independent post-execution observation can VERIFY a claim.
13. **Per-action claims, batch expansion** — `bulk` actions expand so each sub-action gets its own lifecycle, exclusive lease, and claim through the single global Verifier. No vague batch claims.
14. **Typed error taxonomy** — POLICY_DENIED, CONSENT_REQUIRED, OWNERSHIP_CONFLICT, STALE_REFERENCE, BROWSER_NOT_READY, ACTION_FAILED, TIMEOUT, NAVIGATION_CHANGED, VERIFICATION_FAILED, VERIFICATION_UNAVAILABLE, TOOL_NOT_FOUND, SCHEMA_INVALID. Callers decide (retry / replan / ask human / abort) from the code, not from string matching.
15. **Aggregate transaction states** — COMMITTED / PARTIALLY_COMMITTED / FAILED / UNVERIFIED. ROLLED_BACK is reported only if compensating rollback handlers actually run; browser DOM mutations are not ACID, and this is documented as transactional orchestration with explicit partial-commit semantics.
16. **Evidence strength + provenance hierarchy** — command-accepted evidence and bare screenshots can never verify a state postcondition; screenshots require explicit `vision_verified=True`. Unknown postcondition kinds and unknown named verification checks fail closed as UNVERIFIED.
17. **Closed-loop agent** — `/agent/*` endpoints plus the extension's rewritten `runTask()`: observe → update world → select capability → plan → validate → lease → execute one mutation → observe → verify → decide (success | continue | replan | request-human | abort). Step budgets are task-scoped (`TaskExecutionContext`); tasks never share one budget. Refs are pinned to a snapshot version and fail closed on drift; leases release exactly once in `/agent/report`.
18. **`page.loaded` dedup** — snapshots are journaled exactly once via the single bus-event ingest path.

### Stage D (✓): real browser layer
19. **Browser adapter contract** (`harness/browser_runtime.py`) — REAL: one `BrowserRuntime` interface (lifecycle, targeting, observation, interaction, inspection, events) with typed errors, per-tab health states, and a `BrowserAck` that makes `executed=True` inseparable from the post-state observation.
20. **Three honest browser modes** — attached-extension (default; the user's own browser, driven only via extension snapshots + the `/agent/*` closed loop, SYNK never launches or debugs anything), owned-launch (`--use-cdp`; SYNK-managed Chromium with a persistent SYNK-owned profile, explicitly NOT the user's browser), attached-endpoint (`--cdp-endpoint`; connects only to the operator-named CDP endpoint). The old "attach to the user's browser via --remote-debugging-port" implication is gone.
21. **Robust interaction primitives** — `harness/interactions.py` (mirrored in `extension/content.js`): native value setter for controlled React/Vue/Svelte inputs, contenteditable, checkbox/radio, select, keyboard sequences, shadow-DOM traversal, frame targeting with fail-closed mismatch, and post-state observation before replying.
22. **Ref-safe dispatch** — opaque integer refs are resolved through `RefResolver` before reaching any adapter; the old `page.click("3")` bug is gone. Refs carry frame chains and shadow paths and fail closed on drift.
23. **Browser acknowledgements** — the extension's `EXECUTE` handler performs the robust primitive and replies with a `BROWSER_ACK` carrying the observed post-state; the background loop only reports `executed` when the ack says so, and forwards it to `/agent/report`. The server validates the ack (fail closed on `executed=True` without an observation, or on action-id mismatch) and records it as `BROWSER_ACK` evidence: audit trail only, it can NEVER verify a postcondition. Lifecycle: DISPATCHED → BROWSER_ACK → OBSERVED → VERIFIED.
24. **Frame/document identity** — frames carry parent, frame chain, and the browser's own frame id; document identity is preserved across snapshot events and replaced on navigation (so stale refs fail closed).

### Stage E (✓ implementation; live-browser WebMCP UNVERIFIED)
25. **Real WebMCP through page-advertised model context** — discovery is per (session, tab, frame, document); only tools the page's own `navigator.modelContext` advertises are invocable. Handles are sha256-derived from the scope (never Python `hash()`), so a cross-tab handle is unaddressable and a post-navigation handle is stale: both fail closed.
26. **Deterministic capability selection** — `3 * name_hits + desc_hits + schema_bonus` with required-schema-term bonus; ties break by score, risk, then name. `/webmcp/capabilities` returns every candidate, its score breakdown, the winner, and the rationale, all written to the audit trail. Zero-score goals produce no winner, never a silent substitution.
27. **Invocation through the transaction engine** — `/webmcp/invoke` and the `webmcp_invoke` tool run the same REQUEST → VALIDATE → RESERVE → DISPATCH → OBSERVE → VERIFY lifecycle as DOM actions: schema checks, policy/consent gates, per-action claims, `WEBMCP_RESULT` evidence, and `webmcp_result` postcondition verification. A page-reported `ok=True` verifies; a page-reported failure marks the claim FAILED; fixture results can never verify (the verifier refuses `partial_fallback` evidence outright, in both the postcondition and generic paths).
28. **Fail-closed missing tools; honest unavailability** — a tool the page did not advertise fails with `WEBMCP_TOOL_NOT_ADVERTISED` (zero transport calls, zero fallback calls, even with opt-in on). No model context reports `webmcp_unavailable`. A model context with zero tools is reported available (not unavailable) and never enables the fallback.
29. **Opt-in PARTIAL fixture fallback only** — `--webmcp-fallback` wires the legacy fixture table when the page exposes no model context at all; every such result carries the PARTIAL caveat and is labeled PARTIAL wherever it surfaces. The old `/webmcp/execute` endpoint is preserved as this labeled legacy path.
30. **Closed-loop WebMCP** — the extension probes `navigator.modelContext` per page (sync or async listings, plus declarative `script[type="webmcp-tool"]` blocks), pushes the report with snapshots, performs `WEBMCP_INVOKE` in the target frame, and reports the page's own tool outcome to `/agent/report`, which records it as `WEBMCP_RESULT` evidence before verifying (failed reports are recorded too, never silently dropped).

Labels: `CdpModelContextTransport` and the probe JS are REAL code paths (UNVERIFIED against a live page: no `navigator.modelContext` existed in the build environment). `ExtensionSnapshotTransport` is REAL page-reported data (async; synchronous invoke honestly refuses with `BROWSER_NOT_READY`). `FakeModelContextTransport` is a TEST DOUBLE used by `tests/test_stage_e.py` (27 tests) — nothing about its output is presented as a live page. The fixture fallback and `/webmcp/execute` are PARTIAL.

### Stage F (✓): security policy framework + privacy/memory redesign + least privilege

31. **Origin policy, default-deny** (`harness/policy.py`) — REAL: every action's origin must be explicitly registered with allow/deny/consent sets per action class (`read`, `navigate`, `interact`, `write`, `webmcp`) and a minimum trust level. Unknown origins fail closed with typed `POLICY_DENIED` BEFORE lease acquisition; undeterminable origins abstain to the legacy safety path. `GET /policy`, `POST /policy/origin` (typed HTTP 400 on bad input), `POST /policy/origin/remove`.
32. **WebMCP hardening** — REAL: tool names validated as identifiers before touching discovery state; args validated against the tool's DECLARED JSON schema (required, nested, types, `additionalProperties`, `enum`, length, bounds) with `SCHEMA_INVALID` fail-closed; page tool advertisements quarantined (drop + journal with `q_<sha256>` id) on invalid names or injection; handles bound to the discovering principal (session) and trust level (`page-advertised`/`operator-verified`/`operator-trusted`), cross-session invocation fails closed; page results secret-redacted, size-capped, and injection-scanned before evidence; the dead `pass` lease branch in `webmcp/policy.py` is now a real live-lease check threaded from the engine's held lease.
33. **Prompt-injection quarantine** (`harness/contamination.py`, `ContextManager.ingest`) — REAL: deterministic scan of every text-bearing node field; matches replaced with a placeholder BEFORE prompt construction; journal keeps quarantine ids and markers, never hostile text.
34. **Privilege-escalation tracking** — REAL: per-task capability rank; read-to-mutating transitions record a fresh `policy.escalation_review` decision; escalation into denied classes fails closed.
35. **Memory redesign** (`harness/memory.py`) — REAL: scopes (`task` 24h, `session` 7d, `long_term` 365d TTL) with expiry purge; secret redaction BEFORE storage and preference learning that refuses password fields/secret-shaped values; session-isolated reads (session A cannot read session B); physical verified deletion (`POST /memory/delete`, `POST /memory/forget_session`, wipe with post-delete verification); `PRAGMA secure_delete=ON`; journal events carry SHA-256 hashes, never content; legacy plaintext `actions` tables are migrated redacted and dropped.
36. **Least privilege** — REAL: extension manifest reduced to `activeTab` + the local harness host permission (unused `scripting`/`storage` removed); no `eval`/`exec`/`subprocess`/`os.system`/`pickle.loads` anywhere in the execution path (regression-scanned in tests); `__import__("os")` replaced with a normal import; one canonical origin form shared by policy, registry, and WebMCP gateway. See `SECURITY.md` for the full model and residual risks.

Stage F labels: the policy engine, quarantine, schema validation, principal binding, memory redesign, and permission minimization are REAL and covered by `tests/test_stage_f.py` (62 tests). PARTIAL: injection quarantine and secret redaction are pattern-based and can miss novel phrasing/formats (documented in `SECURITY.md` §7). UNVERIFIED: live-browser WebMCP (unchanged from Stage E); memory is not encrypted at rest.

Honest limitations (not hidden):
- No real Chrome/CDP session was available in the build environment: browser behavior was validated through a deterministic fake backend (67 tests in `tests/test_stage_d.py`) plus syntax and contract checks. The Playwright adapter's real-browser integration (launch, attach, crash hooks, frame walking) is implemented but UNVERIFIED against a live browser.
- `dismiss_dialog` in the owned adapter returns the recorded dialog list; it does not dismiss a live dialog (Playwright auto-dismisses only when a handler is registered).
- Frame ids for subframes are content-script-local (`sub:<hash>`); Chrome's numeric `frameId` is recorded when known but cross-frame targeting still relies on the chain.
- WebMCP's `/webmcp/execute` is the legacy Beta.1 fixture path, kept for compatibility and labeled PARTIAL; it can never verify a claim. The Stage E gateway (`/webmcp/discover`, `/webmcp/capabilities`, `/webmcp/invoke`) discovers page-advertised model-context tools per (session, tab, frame, document) and invokes them through the transaction engine with per-action verification.
- Live-browser WebMCP was NOT verified: no page in the build environment exposes `navigator.modelContext`, so discovery/invocation ran against the explicit `FakeModelContextTransport` test double (27 tests in `tests/test_stage_e.py`). The CDP probe JS (`MODEL_CONTEXT_PROBE_JS` / `MODEL_CONTEXT_INVOKE_JS`), the content-script probe, and `OwnedBrowserRuntime.evaluate_js` are implemented but UNVERIFIED against a live page.
- In extension mode the gateway's synchronous path reports UNVERIFIED until the closed loop's post-action observation arrives.
- The mock planner and local model stubs remain deterministic heuristics for offline use; they are labeled as such in responses (`+mock-offline`).

## Safety (Alpha + Beta + Beta.1 §21)

- Fixed tool allowlist; unknown tools ignored. No arbitrary code execution.
- Destructive actions require sidebar consent checkbox.
- Human input pauses the agent; user always wins conflicts (Alpha fallback).
- PII masked in prompts; page text tagged `<untrusted_page_content>`, injection flagged.
- Every decision hash-chained in `/audit`; domain allowlist + banking view-only policy.
- **Beta adds**: ownership + leases + precondition validation before execute.
- **Beta.1 adds**: WebMCP capabilities pass through PolicyEngine (ownership/conflict/consent) before execution.
- **Stage F adds**: per-origin default-deny policy (unknown origins fail closed before lease acquisition); WebMCP tool-name validation, declared-schema validation, advertisement quarantine, principal-bound handles, live-lease verification, and result sanitization; deterministic prompt-injection quarantine of page text before prompt construction; privilege-escalation review journaling; memory scopes with TTL, secret redaction before storage, session-isolated reads, and physically verified deletion; extension permissions minimized to `activeTab` + local harness host. Full model and residual risks in `SECURITY.md`.

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
- [x] **Stage E: real WebMCP** — per-document discovery, page-advertised tools only, deterministic selection with full rationale, invocation through the transaction engine with `webmcp_result` verification, fail-closed missing tools, honest `webmcp_unavailable`, opt-in PARTIAL fixture fallback (never verifies), closed-loop extension path. 27 tests in `tests/test_stage_e.py`. Live-browser WebMCP UNVERIFIED (no `navigator.modelContext` in the build environment; exercised through the `FakeModelContextTransport` test double).
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