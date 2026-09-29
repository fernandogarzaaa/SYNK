# SYNK Security Model (Stage F)

This document is the authoritative statement of SYNK 2.0's security
posture as implemented in this tree. Behavior claims below are backed
by `tests/test_stage_f.py` (62 tests) unless marked otherwise.

## 1. Origin policy: default-deny

`harness/policy.py` (`OriginPolicyRegistry`) is a real per-origin
execution policy, checked by the transaction engine in
`_validate_reserve_precondition` BEFORE any lease is acquired:

- An origin (bare host, e.g. `shop.test`) must be explicitly
  registered via `register_origin()` with allow / deny /
  require_consent sets per action class (`read`, `navigate`,
  `interact`, `write`, `webmcp`) and a minimum trust level.
- An action against an origin that was never registered fails closed
  with typed error `POLICY_DENIED` (state FAILED). Nothing is
  reserved, nothing is dispatched.
- An origin whose URL cannot be determined abstains
  (`POLICY_ABSTAIN`) so the legacy no-page-context path keeps its
  existing safety behavior; it never grants permission.
- Explicit `deny` beats `allow`; `require_consent` classes return
  `CONSENT_REQUIRED` without operator consent.
- `POST /policy/origin` validates input and returns typed HTTP 400
  on bad input; it never leaks a traceback.

Canonical origin form: `policy.origin_of_url()` (bare lowercase host,
no scheme, no port). `WebMCPGateway._origin_for()` uses exactly this
function, so policy registrations match discovery.

## 2. WebMCP capability privilege model

Page-advertised tools are untrusted input. The gateway enforces:

- **Tool-name validation**: names must match `^[A-Za-z][A-Za-z0-9_.\-]{1,64}$`
  before they touch discovery state.
- **Declared-schema validation** (`contamination.validate_input_schema`):
  required fields, nested objects/arrays, primitive types (`bool` is
  not an `integer`), `additionalProperties: false`, `enum`, string
  length, numeric bounds. Malformed args fail closed with
  `SCHEMA_INVALID`; non-object args are rejected outright.
- **Advertisement quarantine**: `sanitize_tool_advertisement` drops
  (and journals with a `q_<sha256>` id) any advertisement with an
  invalid name or instruction-injection in name/description. A
  quarantined tool can never be invoked afterwards.
- **Principal binding**: handles carry the discovering principal
  (session id) and a trust level (`page-advertised` default). A
  handle invoked by a different session fails closed with
  `WEBMCP_SCOPE_VIOLATION` (`POLICY_DENIED`).
- **Lease check**: `PolicyEngine.check()` verifies a presented
  `agent_lease` is still live in the lease table; a stale/unknown
  lease fails closed. The transaction engine stamps the held lease
  into the action (`action["lease"]`) and threads it through
  `ToolExecutor._run_webmcp` -> `gateway.invoke`. (Previously this
  branch was an unimplemented `pass`; it is now a real check.)
- **Result sanitization**: page results are secret-redacted and
  size-capped (20k chars) before evidence; result text that looks
  like an instruction is quarantined (journal keeps the markers,
  never the hostile text) and replaced with a placeholder.
- **JS evaluation**: the only `page.evaluate` calls are fixed
  SYNK-owned probe constants (`_CENSUS_JS`, `_SNAPSHOT_JS`,
  `INTERACTION_JS`, `MODEL_CONTEXT_PROBE_JS`,
  `MODEL_CONTEXT_INVOKE_JS`) or JSON-serialized (`_js_literal`)
  locator/spec values; the CDP transport passes tool name/args via
  Playwright's structured `arg`, never string interpolation.
  `window.scrollBy` uses `int()` coercion.
- A static regression test scans `harness/**/*.py` for
  `eval(`/`exec(`/`pickle.load(s)`/`pickle.Unpickler`/`subprocess.`/
  `os.system(` and fails on any hit.

## 3. Injection quarantine (page text)

`ContextManager.ingest` scans every text-bearing node field
(name, value, placeholder, title, aria-label, text, label) with
`contamination.classify_text` (deterministic patterns: ignore-previous-
instructions, tool impersonation, jailbreak language, credential
exfiltration). Matches are replaced with
`[QUARANTINED: instruction-like page text removed]` BEFORE prompt
construction; each replacement emits `security.quarantine` with a
stable `q_<sha256>` id and the matched markers. The raw hostile text
is never written to prompts or the journal.

## 4. Privilege escalation

`OriginPolicyRegistry.check_action` tracks per-task capability rank
(`read` < `navigate` < `interact`/`webmcp` < `write`). A task moving
from a read-only to a mutating capability records a
`policy.escalation_review` journal event with a fresh decision;
escalation into a denied class fails closed.

## 5. Memory: scopes, TTL, redaction, deletion

`harness/memory.py` (`MemoryStore`):

- Scopes: `task` (24h TTL), `session` (7d TTL), `long_term` (365d
  TTL). Expired rows are purged on read.
- Secrets are redacted (`contamination.redact_secrets`: API keys,
  bearer tokens, passwords, URL query tokens, card-like numbers,
  SSNs, generic `name=value` secret shapes) BEFORE storage.
  `learn_from_action` additionally refuses password fields and any
  value whose redaction changes it.
- Reads are session-isolated: `recent(session_id=...)` returns only
  that session's records; an unscoped read returns only unscoped
  records. Session A can never read session B's memory.
- Deletion is physical and verified: `delete_record` and
  `forget_session` re-query after DELETE and raise if any row
  survives; `forget_all` verifies all three tables are empty.
  `PRAGMA secure_delete=ON` overwrites freed pages.
- The journal carries hashes, never content: `memory.stored`,
  `memory.read` (kinds: recent/record/pref/prefs/summary),
  `memory.deleted`, `memory.session_forgotten`, `memory.forgotten`
  emit SHA-256 content hashes with no payload text.
- Migration: a pre-Stage-F `actions` table (plaintext, no session
  identity) is redacted row-by-row into `records` and then DROPPED;
  existing preference values are re-redacted. Events
  `memory.migrated` / `memory.reredacted` record the run.

## 6. Extension permissions (least privilege)

`extension/manifest.json` declares exactly:

- `permissions: ["activeTab"]`: used by `background.js`
  (`chrome.tabs.query`, `chrome.tabs.sendMessage`). The previously
  declared `scripting` and `storage` permissions were removed: no
  extension code used them.
- `host_permissions: ["http://127.0.0.1:18080/*"]`: the local
  harness endpoint the extension reports snapshots to.

A regression test (`TestManifestMinimization`) asserts the manifest
contains no unused permissions and every declared host permission
appears in the extension source.

## 7. Residual risks (honest, not mitigated here)

- **Hostile pages**: quarantine is pattern-based and deterministic;
  novel or obfuscated prompt-injection phrasing can evade it. This
  is a defense-in-depth layer, not a guarantee.
- **Redaction is shape-based**: secrets that do not match any
  pattern (novel token formats, secrets split across fields) can
  persist. Memory is a local SQLite file; it is not encrypted at
  rest.
- **Live browser unverified**: `CdpModelContextTransport` and the
  probe JS are implemented but UNVERIFIED against a live page
  (no `navigator.modelContext` existed in the build environment);
  covered only by the `FakeModelContextTransport` test double.
- **Transport trust**: in attached-extension mode the extension
  reports page snapshots; a compromised extension could lie to the
  harness. The harness never launches or debugs the user's own
  browser; the managed-launch profile is SYNK-owned and separate.
- **Operator endpoints**: `/policy/origin` lets the operator widen
  permissions. There is no authentication on the local harness
  HTTP server; it binds to localhost by design.
