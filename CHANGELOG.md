# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

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
