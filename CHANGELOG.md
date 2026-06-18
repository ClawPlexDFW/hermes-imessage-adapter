# Changelog

## Unreleased

- feat: dedicated `assets/logo.png` (1200×1200) + `assets/logo-small.png` (256×256) — iMessage bubble silhouette with white lightning bolt, electric-blue circuit traces, and a live-pulse dot; generated locally by `scripts/make_logo.py` (Pillow-based, no external image-gen service)
- feat: AI-agent one-shot install prompt at the top of README — copy/paste the block and your Hermes agent runs the full install + verification end-to-end
- test: CI is now hermes-agent-independent — test suite uses `tests/_stubs/` stub modules (`Platform`, `PlatformConfig`, `BasePlatformAdapter`, `MessageEvent`, `SendResult`, `SessionSource`) so the entire 95-test suite runs on a stock `macos-latest` GitHub Actions runner with just `imsg` + Python 3.11 installed
- test: dropped `SKIP_LIVE_TESTS` env var — the previously-gated `TestAuthzPatch` and `TestInstallDryRun` suites now run in CI against checked-in `tests/fixtures/authz_mixin_{pre,post}patch.py` fixtures
- docs: rewrote TESTING.md to document the stub-based architecture, how to regenerate the patch fixtures, and the new full-suite-in-CI guarantee
- fix: `imsg_react` no longer passes invalid `--to` / `--chat-identifier` flags to `imsg react` (those flags are not supported by `imsg` v0.11.1 `react` subcommand)
- fix: platform adapter exposes module-level `CHAT_ROWID_BY_IDENTIFIER` cache populated on every inbound message, so the `imsg-tools` plugin can resolve a non-numeric `chat_id` to the integer rowid required by `imsg react --chat-id`
- test: added regression tests for the rowid cache (inbound populates, no-rowid does not pollute) and for the new `imsg_react` handler (numeric chat_id path, cache-resolved identifier path, unknown-identifier error path)

## 0.1.0 — 2026-06-18

- Initial public release: IMSG platform adapter + `imsg-tools` plugin (reactions + attachments) + 92-test verification suite + CI workflow
