# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [2.1.1] - 2026-10-13

### Security
- Any web page open in the user's browser could control the harness. It is
  unauthenticated on 127.0.0.1:18080 and answered every request with
  `Access-Control-Allow-Origin: *`. A "simple" `text/plain` POST needs no
  CORS preflight, so a page could register itself in the default-deny origin
  policy (`/policy/origin`), drive `/act` / `/transact` / `/agent/*`, or read
  `/memory`, `/audit` and `/world`. A DNS-rebinding page could read them too.
  The harness now returns 403 for a non-loopback `Host`, and for web origins
  on every path except the content-script pushes (`POST /snapshot`, `/event`).
  CORS is reflected only to extension, Tauri and loopback origins (plus
  `SYNK_TRUSTED_ORIGINS`). Requests without an `Origin` (curl, the Tauri
  shell's Rust client) are unchanged.

## [2.1.0] - 2026-10-13

### Fixed

- `/act` no longer drops the TCP connection; the `/plan` mock is labeled as a mock (#34).
- README test counts brought up to date (#33).
- Live-extension test skips with a clear message when Chromium cannot launch (#16).
- Stale Playwright pin relaxed in `harness/requirements.txt` (#17).

### Changed

- Desktop shell moved to **Tauri 2** (`tauri` 2, `tauri-build` 2.7, `@tauri-apps/api` / `cli` 2.12,
  `tauri.conf.json` v2 schema, `invoke` from `@tauri-apps/api/core`), React 19, Vite 8,
  `@vitejs/plugin-react` 6, `lucide-react` 1.x; CI builds the frontend and `cargo check`s the shell.
- Rust: `tokio` 1.53.2, `reqwest` 0.13.

### Added

- Dependabot configuration for pip (`harness/`), npm and cargo (`shell/`), and GitHub Actions (weekly).
- `.env.example` listing the environment variables SYNK reads (names only); local `.env` files are now git-ignored.
- This CHANGELOG.
