# Phase 3: Controlled Chromium — feasibility + plan

## Verdict

Feasible but deliberately deferred. Nothing in the current architecture
*requires* a fork: extension mode (MV3, CSP-exempt content scripts) and
CDP mode (Playwright, `--remote-debugging-port`) cover the vision doc's
compatibility section (§9). A fork buys: startup control, built-in harness
channel (no localhost REST), custom WebMCP surface, stealth defaults.

## Costs (honest)

- Build system: Chromium takes hours to compile even on strong hardware;
  requires depot_tools + ~100 GB disk. This box (E:, 91 GB free) cannot
  hold a Chromium checkout.
- Maintenance: rebasing patches onto a moving Chromium is a standing tax.
- Distribution: installer signing, auto-update, WebView alternative already
  ships via Tauri (WebView2) with none of this cost.

## Recommended approach (when justified)

1. **Do not fork Blink.** Ship a *managed Chromium* instead: pinned
   `chrome-headless-shell` / full Chromium + `--remote-debugging-port`
   driven by the existing `BrowserController`. This is 90% of the benefit
   (dedicated profile, proxy/flag control, stealth args) at ~0 maintenance.
2. Prototype: extend `harness/browser.py` with a launcher that owns the
   browser subprocess (user-data-dir, proxy PAC for CSP/header rewriting
   per §9, `--disable-blink-features=AutomationControlled`).
3. Only if managed-Chromium proves insufficient: evaluate CEF (Chromium
   Embedded Framework) inside the Tauri shell, replacing WebView2 for the
   agent-driven view while keeping the native UI chrome.
4. Full fork is a last resort, gated on: a paying enterprise need + a build
   machine with 150 GB+ disk + someone to own rebases.

## Exit criteria for starting

- Managed-Chromium launcher merged + EVE regression run against it.
- Documented gap that flags/proxy/CDP provably cannot close.
