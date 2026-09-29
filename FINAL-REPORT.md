# SYNK 2.0: Program Completion Report

**Repository:** `fernandogarzaaa/SYNK` (branch `master`)
**Program:** 23-phase implementation mandate, delivered in 8 merge-gated
stages (A through H), one PR at a time, merged only on green CI.
**License:** MIT.
**Date:** 2026-09-29.

## 1. Thesis

SYNK is a transactional shared-state runtime for human-agent interaction
with the web.

**Central invariant:** an action is successful only after independent
observation satisfies its declared verification contract, never because
the planner, model, or executor returned success.

Everything in this program exists to make that invariant mechanical
rather than aspirational: the lifecycle that forces an observation step,
the evidence hierarchy that refuses weak evidence, the verifier that is
independent of the acting model, and the aggregate states that report
partial truth instead of rounding it up.

## 2. What was built, stage by stage

### Stage A: Clean, canonical, green master (PRs #1, #2, #3)

Established `master` as the only canonical branch; removed tracked
bytecode; fixed the Tauri CI dependency; added `SECURITY.md`; added the
MIT license (copyright 2026 Fernando Garza); deleted stale `main` and
merged feature branches. Master CI confirmed green before any feature
work began.

### Stage B: Runtime identity, event-sourced WorldState, fail-closed refs, exclusive leases (PR #4)

- Canonical IDs and SHA-256 stable identity; canonical origins; the
  Session > windows > tabs > frames > documents hierarchy.
- Event-sourced `WorldState`: append-only journal, deterministic
  reducer, hash-chained audit trail.
- Structured fail-closed `ElementRef`: tab, frame, origin, version,
  fingerprint, frame-chain, and shadow-path checks. Any drift fails
  closed.
- Exclusive, synchronized leases: bounded TTL, hierarchy-aware,
  compare-and-release, plus a separate emergency stop.
- The extension pins the task tab; the server stamps session/tab/task
  identity on every request.

### Stage C: Transaction engine, per-action verification, closed-loop agent (PR #5)

- `TransactionEngine` with the honest lifecycle: REQUEST > VALIDATED >
  LEASED > PRECONDITION_CHECK > DISPATCHED > ACKNOWLEDGED > OBSERVING >
  VERIFIED, and a 12-code typed error taxonomy that distinguishes
  execution failure from verification failure from policy denial.
- Per-action claims: batches no longer share one claim; every action
  gets its own request, execution, evidence, and verification, with
  aggregate `COMMITTED` / `PARTIALLY_COMMITTED` / `FAILED` /
  `UNVERIFIED` (documented as transactional orchestration with explicit
  partial-commit semantics, never called ACID).
- `ExecutionGateway`: the single canonical execution path; `/act` and
  `/transact` are thin adapters over it.
- Closed-loop agent (`/agent/begin|observe|next|report|status`):
  observe > plan > validate > lease > execute > observe > verify >
  decide, with task-scoped budgets and a request-human circuit breaker.
- Evidence hierarchy with strength and provenance: command-accepted
  evidence can never verify; screenshots need a dedicated vision
  result; unknown verifiers and postconditions fail closed.

### Stage D: Real browser adapters and robust interaction primitives (PR #6)

- `BrowserRuntime` contract: lifecycle, targeting, observation,
  interaction, inspection, events, typed errors, per-tab health.
  `BrowserAck(executed=True, observed=None)` raises by construction;
  dishonest acks fail the execution and are recorded.
- Attached-extension mode: actions run through the closed loop on the
  pinned tab; the ack must carry observed post-state.
- Owned-browser mode with honest separation: `owned-launch`
  (SYNK-managed Chromium, persistent SYNK-owned profile, never the
  user's browser) vs `attached-endpoint` (operator-named CDP endpoint
  only). Playwright is optional and lazily imported.
- Robust interaction primitives replacing raw DOM mutation: native
  value setters for controlled inputs, contenteditable, select,
  checkbox/radio, keyboard sequences, shadow-DOM traversal,
  fail-closed ambiguous locators, post-state observation.
- `RefResolver`: an integer ref never reaches an adapter as a selector.

### Stage E: Real WebMCP gateway and capability selection

Page model-context tools: discovery, handle scoping bound to
(session, tab, frame, document), declared-schema input validation,
per-origin policy, transport, and a verifier-side `webmcp_result`
postcondition. Fixture fallback is explicit, labeled PARTIAL, and can
never verify a claim.

### Stage F: Security, privacy, and memory hardening

Per-origin default-deny policy with privilege-escalation review and
consent gates; injection quarantine for page text; secret redaction
before persistence; task-scoped, session-isolated memory with real
forget/delete; WebMCP capability privilege model. Full model in
`SECURITY.md`; 40 adversarial tests in `tests/test_adversarial.py`
attack every entry point and assert the fail-closed outcome.

### Stage G: Scheduler, workflow learning, routing, compiler

Sequential, lease-aware `TaskScheduler` with dependency ordering;
workflow miner + memory with replay/suggest; requirement-based model
routing; task-spec compiler with typed errors. Dispatch correctness is
benchmarked (dependencies, FIFO, tab-lease blocking).

### Stage H: Benchmarks, adversarial suite, Tauri shell, docs, final report (this stage)

- **Benchmarks:** removed the pre-2.0 comparative-agent suite (mock
  nodes, hard-coded timings) and replaced it with five honest
  benchmarks; every number measured; methodology in
  `benchmark/REPORT.md`; raw results regenerated in
  `benchmark/results.json`.
- **Adversarial suite:** `tests/test_adversarial.py`, 40 tests, all
  attacks fail closed (see section 5).
- **Tauri shell:** `cargo check --locked` run locally with a
  freshly installed Rust toolchain (rustc/cargo 1.98.1 stable):
  passes with zero warnings and zero errors (see section 6). The
  check caught one real build-order issue (missing `shell/dist`
  panics `tauri::generate_context!()` at compile time), fixed by
  building the frontend first. Packaged-app bundling remains
  unvalidated (see section 6).
- **Docs:** README rewritten as an operator manual; this report.

## 3. Verification evidence

Final local verification (Stage H, run in full before the PR):

| Check | Result |
|---|---|
| `python -m unittest discover -s tests` | 325 tests, OK |
| `python -m pytest tests/ -q` | 330 passed |
| Live-browser gap verification (2026-09-29, real Chromium) | |
| `python -m pytest tests/live/ -q` | 19 passed (owned launch, CDP attach, WebMCP, scheduler, unpacked extension) |
| `SYNK_LIVE_BROWSER=0 python -m pytest tests/live/ -q` | 19 skipped, graceful (CI browserless path) |
| `python -m compileall -q harness tests benchmark demo` | clean |
| `node --check extension/content.js` | clean |
| `node --check extension/background.js` | clean |
| TODO / FIXME / stub scan | none in shipped code |
| `python benchmark/runner.py` | all 5 benchmarks ok |
| Quickstart (server + snapshot + act + audit) | executed live; audit chain valid |
| Shell: Tauri handler/command parity (CI script) | 5 handlers, all defined |
| Shell: frontend invoke parity (CI script) | all invokes resolve |
| Shell: `tauri.conf.json` validity + version consistency | valid, consistent |
| Shell: `npm ci` + `npm run build` (vite) | 1254 modules, built in 3.54s |

Benchmark headlines (measured, this machine, Python 3.12.3):

- Gateway throughput: 1663.71 actions/sec; mean 0.601 ms, p99 0.285 ms
  (harness overhead in extension mode; all 200 actions honestly
  UNVERIFIED with no browser attached)
- Verifier accuracy: 19/19 declared claim/evidence pairs correct
- Lease contention: 8 threads x 50 attempts, 0 exclusivity violations
- Policy check: mean 3.16 us, p99 5.65 us
- Scheduler dispatch: dependency order, FIFO, and tab-lease blocking
  all correct; mean dispatch 0.029 ms

CI history: PRs #1 through #6 each merged only after all checks green
(Python suite + Tauri shell job on ubuntu-22.04, which runs `cargo
check`, the vite build, and both parity scripts).

## 4. The invariant, restated as mechanism

The invariant is not a comment; it is six enforced rules:

1. The engine's lifecycle has no path from DISPATCHED to VERIFIED that
   skips OBSERVING.
2. `postcondition_for` declares the contract per action; the verifier
   accepts only evidence types in `POSTCONDITION_EVIDENCE[kind]`.
3. `BROWSER_EVENT` and `BROWSER_ACK` are excluded from every
   postcondition allowlist; `BrowserAck` with `executed=True` and no
   observation is unrepresentable (constructor raises).
4. Screenshots verify only with `vision_verified: True`.
5. Unknown postcondition kinds and unknown named checks fail closed as
   UNVERIFIED.
6. Execution failure returns `FAILED` with an `ACTION_FAILED`-family
   code and proposes no claim; verification failure is a separate
   outcome on an acknowledged execution.

## 5. Adversarial coverage (what was attacked)

`tests/test_adversarial.py` (40 tests). Each test asserts the recorded
fail-closed outcome:

- Classic prompt injection in snapshot node names and tool
  descriptions: quarantined; the planner sees a placeholder; the audit
  trail keeps ids + markers, never the hostile payload.
- Unicode-obfuscated injection (Cyrillic/Greek homoglyphs,
  zero-width characters, fullwidth Latin): caught by the hardened
  classifier, which additionally scans an NFKC-normalized view of
  each page string with invisible characters stripped and
  confusables folded to Latin; text carrying zero-width/invisible
  characters is flagged even when no pattern matches. Quarantined
  like classic injection; the architectural invariant still holds,
  page strings can never be dispatched as tool calls
  (`TOOL_NOT_FOUND` fail-closed).
- Secrets smuggled in tool results: redacted; oversized results
  truncated; results are evidence, never re-dispatched.
- WebMCP inputs violating declared schemas: rejected; malformed tool
  names quarantined.
- Policy escalation (read then mutate): triggers a fresh recorded
  escalation review; unknown origins denied; denied classes denied;
  consent-required classes without consent refused.
- Cross-tab and cross-session WebMCP handle reuse: scope mismatch;
  post-navigation document change invalidates handles.
- Stale ref reuse after navigation, wrong-tab refs, wrong-origin refs:
  `STALE_REFERENCE`, action `CONFLICTING`, no claim proposed.
- Lease double-spend: second acquire refused; foreign release refused;
  double release refused; emergency stop blocks all acquisition.
- Dishonest acks: constructor rejects bare `executed=True`; the agent
  loop rejects empty-observation and action-id-mismatched acks; honest
  acks are recorded but can never verify.
- Evidence-strength attacks: weak evidence alone never verifies;
  contradictory evidence yields `CONFLICTING`/`FAILED`.

## 6. Known limitations and unverified items

Stated plainly; nothing here is presented as done:

1. **Live-browser gap closed (2026-09-29, PR "live browser
   verification").** The paths below were exercised against a real
   Chromium (Chrome for Testing 153.0.8010.12, Playwright-driven,
   SYNK-owned profile; the harness never touches the user's browser)
   and are covered by 19 tests in `tests/live/`, which skip gracefully
   in browserless CI (`SYNK_LIVE_BROWSER=0` forces the skip):
   - Owned Chromium launch: real typing, clicking, navigation,
     snapshots, and acks through the transaction engine. Ack-only
     evidence stays UNVERIFIED; fresh independent snapshot evidence
     verifies; contradictory evidence stays UNVERIFIED.
   - Explicit attach to an operator-started
     `--remote-debugging-port=0` Chromium endpoint, with endpoint
     discovery via `DevToolsActivePort`/`/json/version` and
     fail-closed rejection of non-WebSocket endpoints.
   - Live-page `navigator.modelContext` WebMCP: discovery of
     advertised tools, deterministic selection, invocation, and
     verification from the page-reported `WEBMCP_RESULT`; missing
     model context and unadvertised tools fail closed.
   - Sequential scheduler driving a real browser: completes only when
     every action verifies against fresh evidence; one task per
     `run_next()`; dispatched-but-unverified actions yield `failed`,
     never `completed`.
   - Unpacked extension: real snapshot push ingested by the harness
     and live `value.changed` event reporting from the content script.
   Live verification found and fixed three shipped bugs: an
   owned-browser lifecycle deadlock (loop thread vs. lock), a
   Playwright `evaluate()` multi-argument crash in the type/select
   primitives, and an immediately-invoked WebMCP transport function
   that never received its arguments; it also required CORS handling
   on the loopback harness and an `ignore_default_args` option so the
   unpacked extension is not disabled by Playwright defaults.
   Still unmeasured: real-page verification rates and interaction
   timings at scale, and crash/restart hooks against a real browser.
   The WebMCP fixture page is scaffolding: it verifies the real
   transport and runtime path, not interop with every external WebMCP
   implementation.
2. **Tauri shell: `cargo check` now run locally (2026-09-29).** A
   Rust toolchain was installed (rustc/cargo 1.98.1, stable) and
   `cargo check --locked` in `shell/src-tauri` passes with zero
   warnings and zero errors. Two environment notes, both verified
   the hard way: the frontend must be built first (`npm run build`
   in `shell/`), because `tauri::generate_context!()` panics at
   compile time when `shell/dist` is missing; and on Ubuntu 24.04
   the locked Tauri 1.8.3 / wry 0.24.12 / webkit2gtk-sys 0.18.0
   tree probes for WebKitGTK 4.0 pkg-config names
   (`webkit2gtk-4.0`, `javascriptcoregtk-4.0`) that the distro no
   longer ships (only 4.1 dev packages exist), so the check ran
   with a local-only pkg-config shim mapping the 4.0 names onto
   the installed 4.1 packages (no repo change); linking
   additionally needed `libwebkit2gtk-4.0.so` /
   `libjavascriptcoregtk-4.0.so` names, provided as symlinks to the
   installed 4.1 libraries (environment only). A full
   `cargo build --locked` then links cleanly into a working
   `synk-shell` binary, which was smoke-tested under Xvfb against a
   running harness: it boots, creates its window, and runs with no
   panics (only benign headless-GPU and WebKit deprecation
   warnings). Installer packaging (`npm run tauri build`) and
   click-through exercise of the five Tauri commands remain
   unvalidated, so the shell stays labeled EXPERIMENTAL.
   Previously verified items still stand:
   handler/command parity, frontend invoke parity,
   `tauri.conf.json` validity and version consistency, and a full
   vite production build.
3. **Unicode-obfuscation detection is pattern-bound, not
   universal** (see section 5). The hardened classifier catches
   NFKC-foldable forms (fullwidth Latin), the listed
   zero-width/invisible characters, and Cyrillic/Greek homoglyphs
   from its confusable map when they spell a known injection
   pattern. Still out of scope: homoglyphs outside the map (for
   example Cherokee or Armenian lookalikes), invisible codepoints
   not in the list, obfuscated phrasings that match no known
   pattern, and visual-only attacks (text rendered in images).
   Mitigation remains architectural as well: page text is never
   instruction, regardless of detection.
4. **The planner is a local heuristic, not a model.** Cloud planning
   requires operator-configured credentials (`docs/local-models.md`);
   the mock planner path is labeled as such wherever it appears.
5. **No rollback handlers exist.** `ROLLED_BACK` is defined but never
   produced; partial commits are reported, not compensated.
6. **`dismiss_dialog()` in the owned adapter** returns the recorded
   dialog list; dismissing a live dialog is not implemented.
7. **Benchmarks are single-machine, unloaded measurements.** They bound
   harness overhead; they are not production SLOs.

## 7. Residual risks

- A compromised or buggy page could still waste agent budgets with
  adversarial DOM churn; budgets bound the cost but the churn itself
  is not classified as an attack.
- The quarantine classifier's documented limits (item 3 above) mean a
  sufficiently motivated page can get hostile text into the
  planner's context window as *data*; the system treats it as data,
  but prompt-level confusion in a future model-backed planner is a
  risk that must be re-tested whenever the planner changes.
- Lease TTLs are bounded but clock-dependent; extreme clock skew
  between components has not been tested.
- The audit journal is hash-chained in memory; durable tamper-evidence
  across restarts depends on the operator persisting it.

## 8. What a future operator must still do

1. Re-run `python benchmark/runner.py` with a browser-attached
   throughput/verification-rate benchmark; label the new numbers with
   the browser, version, and page set used. (Real-page verification
   rates and interaction timings remain UNMEASURED.)
2. Exercise crash/restart hooks against a real browser session.
3. Package the Tauri shell (`npm run tauri build` with a Rust
   toolchain) and smoke-test the five commands against a running
   harness before calling the shell anything stronger than
   EXPERIMENTAL.
4. For WebMCP interop beyond the fixture page: point
   `/webmcp/discover` + `/webmcp/invoke` at a real third-party
   model-context implementation and confirm the `webmcp_result`
   postcondition still verifies from the page's own report.

## 9. Completion statement

SYNK 2.0 is complete per the mandate: inspected before each stage,
implemented in coherent stages, regression-tested at every stage, run
and fixed, documented, and merged only on green CI. The central
invariant is enforced by mechanism, attacked by 40 adversarial tests,
and measured by 5 honest benchmarks. What could not be verified is
enumerated above, not hidden. No stubs, no TODOs, no invented numbers
ship in this tree.
