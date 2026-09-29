# SYNK: Transactional Shared-State Runtime for Human-Agent Web Interaction

SYNK is a runtime that lets a human and an AI agent work the same browser
session concurrently, with every agent action executed as a supervised
transaction and verified against independent observation.

**Central invariant:** an action is successful only after independent
observation satisfies its declared verification contract. Never because
the planner, model, or executor returned success.

## Architecture

```
                    +------------------ page-supplied strings are data, never instructions
                    |
   +------------+   v   +-----------+   +------------------+   +-----------+
   |  Browser   |-----> | Snapshot  |-->| ContextManager    |-->| Planner   |
   | (extension | nodes | /snapshot |   | (refs, versions,  |   | (local /  |
   |  or CDP)   |<----- |           |   |  quarantine)      |   |  cloud)   |
   +------------+  ack  +-----------+   +------------------+   +-----------+
         ^                                                        |
         | EXECUTE (robust primitives)                            v actions
         |                                              +------------------+
         |                                              | ExecutionGateway |
         |                                              |  .execute()      |
         |                                              +--------+---------+
         |                                                       |
         v                                                       v
   +-----------+   +-----------+   +--------+   +---------+   +----------+
   | Browser   |-- | Ownership |-- | Policy |-- | Verifier |-- | Audit    |
   |Runtime    |   | (leases)  |   | (allow |   | (claims  |   | journal  |
   | adapter   |   |           |   |  list) |   |  + evid.)|   | (hash-   |
   +-----------+   +-----------+   +--------+   +---------+   | chained) |
                                                            +----------+
```

One action's lifecycle through the gateway:

```
REQUEST -> VALIDATED -> LEASED -> PRECONDITION_CHECK -> DISPATCHED
  -> ACKNOWLEDGED -> OBSERVING -> VERIFIED | FAILED | UNVERIFIED
```

Aggregate transaction status: `COMMITTED`, `PARTIALLY_COMMITTED`,
`FAILED`, `UNVERIFIED`. This is transactional orchestration with
explicit partial-commit semantics, not ACID: browser DOM mutations
cannot be rolled back, and the system never claims otherwise.

## Subsystem status

Every subsystem carries one honest label:

| Subsystem | Status | Notes |
|---|---|---|
| Transaction engine (`harness/transactions.py`) | REAL | Per-action lifecycle, typed errors, fail-closed unknown verification |
| Execution gateway (`harness/gateway.py`) | REAL | Single entrypoint for /act, /transact, /agent/*, shell, benchmarks |
| Event-sourced WorldState | REAL | Append-only journal, deterministic reducer, hash-chained audit |
| Exclusive leases (`harness/concurrency.py`) | REAL | Compare-and-release, TTL, hierarchy, emergency stop; verified under thread contention |
| Fail-closed element refs | REAL | tab/frame/origin/version/fingerprint/frame-chain/shadow-path checks |
| Independent verifier | REAL | Evidence-strength hierarchy; command-accepted evidence can never verify |
| Policy framework (`harness/policy.py`) | REAL | Per-origin default-deny allowlists, escalation review, consent gates |
| Contamination guards (`harness/contamination.py`) | REAL | Injection quarantine, secret redaction, schema validation; unicode-obfuscation limits documented |
| Task scheduler (`harness/task_scheduler.py`) | REAL | Sequential, lease-aware, dependency-ordered; dispatch correctness benchmarked |
| Workflow learning | REAL | Miner + memory, replay/suggest endpoints |
| Model router / compiler | REAL | Requirement-based routing, task-spec compiler with typed errors |
| Closed-loop agent (`/agent/*`) | REAL | Observe > plan > validate > lease > execute > observe > verify > decide |
| Extension (attached mode) | PARTIAL | Content script runs robust primitives and returns honest acks; real-Chrome end-to-end UNVERIFIED here |
| Owned-browser adapter (Playwright/CDP) | PARTIAL | Implemented with honest mode separation; live-browser integration UNVERIFIED here |
| WebMCP gateway | PARTIAL | Discovery, scoping, schema validation, policy; page model-context path needs a live page (UNVERIFIED here) |
| Memory store | REAL | Task-scoped, session-isolated, secrets redacted before storage |
| Tauri shell (`shell/`) | EXPERIMENTAL | Thin HTTP client over the harness API; Rust compile + prod build verified in CI, not run to a packaged app here |
| Benchmarks (`benchmark/`) | REAL | All numbers measured; methodology in `benchmark/REPORT.md` |

Labels: REAL (implemented, tested, exercised), PARTIAL (real code with an
explicitly unverified integration), EXPERIMENTAL (works in CI, not
production-hardened), TEST DOUBLE (a fake stands in for an external
system), UNVERIFIED (not exercised in this environment).

## Quickstart

Prerequisites: Python 3.11+, `pip install pytest`. No browser needed for
the harness path below. Every command here was executed against the
local harness during Stage H verification.

```bash
# 1. Start the harness (extension mode; no browser dependency)
python -m harness.server --port 18080

# 2. In another terminal: register the page origin
#    (Stage F policy is default-deny: unknown origins fail closed)
curl -X POST http://127.0.0.1:18080/policy/origin \
  -H 'Content-Type: application/json' \
  -d '{"origin":"demo.shop","allow":["read","navigate","interact"]}'

# 3. Push a page snapshot (what the extension's content.js captures)
curl -X POST http://127.0.0.1:18080/snapshot \
  -H 'Content-Type: application/json' \
  -d '{"url":"https://demo.shop/checkout","tab_id":"t1","goal":"Fill the form",
       "nodes":[{"role":"textbox","name":"Email","tag":"input",
                 "selector":"#email","interactive":true}]}'

# 4. Execute one action through the gateway
curl -X POST http://127.0.0.1:18080/act \
  -H 'Content-Type: application/json' \
  -d '{"actions":[{"tool":"type","selector":"#email","text":"inan@example.com"}],
       "tab_id":"t1","page_url":"https://demo.shop/checkout","task_id":"qs1"}'
# -> transaction_status: UNVERIFIED (honest: no browser observation yet)

# 5. Inspect the hash-chained audit trail
curl http://127.0.0.1:18080/audit
```

Browser modes (need a real browser; not exercised in CI):

```bash
# SYNK-owned Chromium, persistent SYNK profile (never the user's browser)
# requires: pip install playwright && playwright install chromium
python -m harness.server --port 18080 --use-cdp

# Attach to an operator-named CDP endpoint (explicit, never implied)
python -m harness.server --port 18080 --cdp-endpoint ws://127.0.0.1:9222/devtools/browser/<id>
```

Tests and benchmarks:

```bash
python -m unittest discover -s tests        # 325 tests
python -m pytest tests/ -q                  # 330 tests
python benchmark/runner.py                  # regenerates benchmark/results.json
```

## Endpoint reference

Base URL defaults to `http://127.0.0.1:18080`. All POST bodies are JSON.

**Reads (GET):** `/health`, `/world`, `/audit`, `/memory`, `/ladder`,
`/workflows`, `/workflows/learned`, `/models`, `/policy`,
`/webmcp/capabilities`.

**Execution (POST):**

| Endpoint | Purpose |
|---|---|
| `/snapshot` | Ingest a page snapshot; returns versioned element refs (page text quarantined on the way in) |
| `/plan` | Plan actions for a goal against the pinned tab's snapshot |
| `/act` | Execute actions through the gateway (thin adapter; per-action results + verifications) |
| `/transact` | Execute an explicit transaction (same gateway, caller-supplied transaction id) |
| `/agent/begin` | Start a closed-loop task (pins the tab; task-scoped budgets) |
| `/agent/observe` | Push a fresh observation into the task |
| `/agent/next` | Get the next planned action for the task |
| `/agent/report` | Report execution with a browser ack (dishonest acks rejected) |
| `/agent/status` | Task state, budgets, verification counts |
| `/event` | Ingest a browser event (navigation, dialog, download, network) |
| `/lease` | Inspect active leases |
| `/estop` | Emergency stop: refuse all new lease acquisitions |

**Scheduling / workflows (POST):** `/task/submit`, `/task/poll`,
`/task/cancel`, `/task/run`, `/compile`, `/route`,
`/workflow/observe`, `/workflow/suggest`, `/workflows/learn`,
`/workflows/get`, `/workflows/suggest`, `/workflows/replay`,
`/workflows/learned`.

**Verification (POST):** `/verification/claim`, `/verification/verify`,
`/verification/evidence`, `/vision/capture`.

**WebMCP (POST):** `/webmcp/discover`, `/webmcp/invoke`,
`/webmcp/execute` (+ `GET /webmcp/capabilities`).

**Memory (POST):** `/memory/pref`, `/memory/forget`, `/memory/delete`,
`/memory/forget_session` (+ `GET /memory`).

**Policy (POST):** `/policy/origin`, `/policy/origin/remove`
(+ `GET /policy`).

**Misc (POST):** `/human` (human input/interrupt), `/benchmark`
(synthetic headless benchmark), `/models` (model routing info).

## Verification contract

1. Every mutating action declares a postcondition (`element_value`,
   `element_interaction`, `url`, `webmcp_result`).
2. Only evidence types appropriate to that postcondition may verify it
   (`harness/verification/evidence.py: POSTCONDITION_EVIDENCE`).
3. Command-accepted evidence (`BROWSER_EVENT`, `BROWSER_ACK`) is
   audit-trail only: it can never verify, by construction.
4. Screenshots verify only with a dedicated vision-verification result.
5. Unknown postcondition kinds and unknown named checks fail closed as
   `UNVERIFIED`.
6. Execution failure (`FAILED`) is never reported as verification
   failure: if the tool call itself fails, no claim is proposed.

## Security model

Summary; the full model is in `SECURITY.md`.

- **Default-deny policy:** every page origin must be registered with an
  explicit allowlist before any action runs there. Unknown origins fail
  closed. Privilege escalation (read -> mutate) triggers a fresh,
  recorded policy review.
- **Page text is untrusted data:** node text, tool advertisements, and
  tool results pass through contamination guards. Injection-bearing
  strings are quarantined (placeholder in the planner's view, id +
  markers in the audit trail, never the hostile payload). Secrets are
  redacted before anything is persisted or echoed.
- **Capability least privilege:** WebMCP handles are bound to
  (session, tab, frame, document); cross-tab, cross-session, and
  post-navigation reuse fail closed. Tool names must be clean
  identifiers; inputs are schema-validated.
- **Human priority:** exclusive leases serialize agent/human access to
  the same target; the human always wins conflicts. Emergency stop
  refuses all new acquisitions.
- **Memory privacy:** records are task-scoped and session-isolated;
  the journal stores content hashes, not content; forget/delete
  endpoints actually delete.
- **Adversarial suite:** `tests/test_adversarial.py` (40 tests) attacks
  every entry point above and asserts the fail-closed outcome.

## Benchmarks

`python benchmark/runner.py` regenerates `benchmark/results.json`.
Methodology and literal numbers: `benchmark/REPORT.md`. Headlines from
the Stage H run (Python 3.12.3, this machine):

- Gateway throughput: 1663.71 actions/sec (harness overhead, extension
  mode, no browser)
- Verifier accuracy: 19/19 declared claim/evidence pairs correct
- Lease contention: 8 threads x 50 attempts, 0 exclusivity violations
- Policy check: mean 3.16 us, p99 5.65 us
- Scheduler dispatch: dependency order, FIFO, and tab-lease blocking
  all correct; mean dispatch 0.029 ms

## What this is not

- **Not ACID transactions.** Browser DOM mutations cannot be rolled
  back. The aggregate states (`PARTIALLY_COMMITTED`, `UNVERIFIED`)
  exist precisely to report partial truth.
- **Not a parallel scheduler.** `TaskScheduler` dispatches sequentially;
  "parallel" in older docs meant multi-tab task tracking, not concurrent
  execution.
- **Not verified against a live browser in this environment.** The
  extension content script, the Playwright/CDP adapter, and the page
  model-context WebMCP path are implemented and unit-tested against a
  deterministic fake backend, but no real Chrome/CDP session was
  available here. `FINAL-REPORT.md` lists exactly what a future
  operator must do to verify the live-browser path.
- **Not a production browser.** The Tauri shell is a thin operator UI
  over the harness HTTP API, EXPERIMENTAL, not a hardened product.
- **No invented metrics.** Every number in `benchmark/REPORT.md` was
  measured by running `benchmark/runner.py`; removed legacy benchmark
  files existed only to compare mock agents on hard-coded timings.

## Repository layout

```
harness/            Python runtime (stdlib only; playwright optional)
  gateway.py        Single execution entrypoint
  transactions.py   Per-action lifecycle + typed errors
  verification/     Claims, evidence, results, the independent Verifier
  browser_*.py      BrowserRuntime contract + attached/owned adapters
  interactions.py   Robust interaction primitives
  policy.py         Per-origin default-deny policy + escalation review
  contamination.py  Injection quarantine, secret redaction, schema checks
  task_scheduler.py Sequential lease-aware scheduler
  webmcp/           Page model-context gateway (discovery, scoping, invoke)
  memory.py         Task-scoped, session-isolated memory
  server.py         HTTP API (the operator surface)
extension/          Chrome extension (attached mode)
shell/              Tauri desktop shell (EXPERIMENTAL)
benchmark/          Honest benchmarks + REPORT.md + results.json
tests/              330 pytest / 325 unittest, incl. adversarial suite
demo/               Headless demo against the local harness
docs/               Local-model setup and other operator docs
SECURITY.md         Full security model
FINAL-REPORT.md     SYNK 2.0 program completion report
```

## License

MIT. See `LICENSE`.
