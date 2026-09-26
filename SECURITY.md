# Security policy

## Reporting a vulnerability

Report privately through
[GitHub security advisories](https://github.com/fernandogarzaaa/SYNK/security/advisories/new).
Please do not open a public issue for a vulnerability.

Include what you would need to reproduce it yourself: version or commit,
component (extension, harness/server, Tauri shell), and a minimal case.
You should get an initial response within a week.

## Supported versions

SYNK is pre-release software. Only the latest commit on the default branch
receives security fixes. Upgrade first, then report if the issue persists.

## Scope notes

A few things about SYNK's design are worth knowing before reporting:

- **Agents act on live web sessions.** SYNK runs a collaborative browser
  runtime where humans and AI agents operate concurrently on the same
  session. An agent's actions (navigation, form fills, clicks) execute
  against real web applications. Run it against applications you trust,
  and treat the action log as the source of truth for what an agent did.
- **Inspected pages are untrusted input.** Content scripts capture page
  content into the world state that agents reason over. A malicious page
  can try to influence agent behavior through its content; treat page
  text as data, never as instructions.
- **Model keys live in local configuration.** The harness calls cloud
  models with keys from your environment or config file. Do not commit
  keys, and do not share harness logs that may contain them.
- **The local server binds to loopback.** The harness listens on
  127.0.0.1. Do not expose it to a network; the action-execution
  endpoints assume a trusted local operator.
