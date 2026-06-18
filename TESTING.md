# Testing

This adapter ships with a **95-test pytest suite** that exercises the
real adapter code in isolation from Hermes. The suite runs in any
environment with Python 3.11+ and the `imsg` CLI — no live Hermes
install required.

## What the tests cover

| Suite                       | What it verifies |
| --------------------------- | ---------------- |
| `TestHelpers`               | `_redact`, `_normalize_phone`, `_discover_imsg_path`, `check_imsg_requirements`, `VALID_REACTIONS` constant |
| `TestUnicodeRendering`      | `render_imessage()`: bold/italic/mono/strikethrough/link conversion to iMessage-flavored Unicode; snake_case not mangled; code blocks protected; emoji preserved |
| `TestWatchLineParsing`      | The actual `imsg watch --json` JSON shape parses correctly; `is_from_me` self-loop guard; dedup; empty/attachment/malformed messages dropped; group chat routing (is_group → chat_type); tapback reaction events forwarded to `handle_reaction` (not emitted as text) |
| `TestRPCSend`               | Outbound `imsg rpc` JSON-RPC 2.0: schema, `service: "imessage"`, 4000-char truncation, error propagation, numeric `chat_id` → `chatId`, `unicode_format` default-on |
| `TestSendFile`              | Outbound attachments via `imsg send --file`: handle-routes-to, numeric-routes-to-chat-id, nonexistent-path rejection, caption-then-file order |
| `TestReact`                 | Outbound tapback: invokes `imsg react` subprocess (not rpc), numeric chat_id required, osascript-permission error → actionable message |
| `TestSendTyping`            | Outbound `imsg typing` subprocess invocation; stop-typing; error-swallowing; chat_id routing for group chats; macOS 26 + SIP detection; csrutil probe |
| `TestRowidCache`            | `CHAT_ROWID_BY_IDENTIFIER` cache populated by `_handle_watch_line`; resolve rowid for non-numeric identifiers used by `imsg-tools` plugin |
| `TestAuthzPatch`            | The bundled `patches/authz.patch` applies cleanly against a checked-in pre-patch fixture and produces a byte-identical post-patch fixture; re-apply is safe with `--forward --batch` |
| `TestInstallDryRun`         | The README's install recipe produces a working adapter (importable from a temp `HERMES_HOME` populated with stub `gateway.*` modules) |
| `TestImsgReactSchema`       | `imsg-tools` plugin `imsg_react` tool schema: required fields, reaction enum |
| `TestImsgSendFileSchema`    | `imsg-tools` plugin `imsg_send_file` tool schema: required fields, optional caption |
| `TestHandleImsgReact`       | Plugin `imsg_react` handler: numeric chat_id → `--chat-id` flag; non-numeric identifier → cache lookup; unknown identifier → error; osascript permission error handling |
| `TestHandleImsgSendFile`    | Plugin `imsg_send_file` handler: empty/missing file rejection, with/without caption |
| `TestPluginRegister`        | `register(ctx)` registers both tools under the expected names |

The tests import the **real** adapter from
`platforms/imsg.py` in this repo (loaded by `tests/conftest.py`), not
the live Hermes install.

## Running the tests

### Locally (any macOS host with imsg)

```bash
cd /path/to/hermes-imessage-adapter
uv run --with pytest --with aiohttp python -m pytest tests/ -v
```

Expected: **95 passed in <1s**.

The suite discovers `imsg` via the standard helper locations
(`/opt/homebrew/bin/imsg` on Apple Silicon, `/usr/local/bin/imsg` on
Intel). Install with `brew install steipete/tap/imsg` if you haven't
already.

### On any Linux/macOS host without imsg

```bash
cd /path/to/hermes-imessage-adapter
uv run --with pytest --with aiohttp python -m pytest tests/ -v
```

All tests except those that actually shell out to `imsg` (the small
number in `TestInstallDryRun` that calls `check_imsg_requirements()`)
pass without imsg installed. If `imsg` is not on `PATH`, the test
runner will mark those few assertions accordingly.

## How the suite stays hermes-agent-independent

The adapter under test does `from gateway.config import Platform,
PlatformConfig` and `from gateway.platforms.base import
BasePlatformAdapter, MessageEvent, ...`. The live `gateway` package is
~200KB of code coupled to the rest of Hermes — pulling it into CI
would require the full Hermes checkout on every runner.

Instead, the test suite ships **stub modules** under `tests/_stubs/`
that mirror only the symbols the adapter imports:

```
tests/_stubs/
└── gateway/
    ├── __init__.py
    ├── config.py            # Platform enum, PlatformConfig dataclass
    ├── session.py           # SessionSource dataclass
    └── platforms/
        ├── __init__.py
        └── base.py          # BasePlatformAdapter, MessageType, MessageEvent, SendResult
```

`tests/conftest.py` puts `tests/_stubs/` at the front of `sys.path`
and then loads `platforms/imsg.py` into the `gateway.platforms.imsg`
module. Tests import `from gateway.config import Platform, PlatformConfig`
exactly as the real adapter does, and the stubs resolve first.

This means:

- **CI runs in 0.2s** instead of pulling a 100MB Hermes checkout
- **No `SKIP_LIVE_TESTS` env var** is required — every test runs
- **The test surface mirrors what the adapter actually uses**, so a
  Hermes core change that breaks the adapter contract still surfaces
  here

If the adapter ever imports a new symbol from Hermes that the stubs do
not cover, the conftest's `assert hasattr(Platform, "IMSG")` style
guards will fail loudly at import time. Extend the stub, don't paper
over it.

## The patch idempotency bug

While writing the tests we caught a real bug: **re-applying
`patches/authz.patch` without `--forward --batch` flags can silently
APPLY IN REVERSE**, removing the lines. The default GNU `patch` behavior
on a non-TTY is interactive; if the patch detects the change is already
applied, it prompts to assume `-R` (reverse), and on EOF can apply
the reverse.

The README install steps use:

```sh
patch -p1 --forward --batch < patches/authz.patch
```

`--forward` ignores patches that appear to be reversed, and `--batch`
makes it non-interactive. This is tested in
`TestAuthzPatch::test_patch_is_idempotent`.

The patch tests use two checked-in fixtures:

- `tests/fixtures/authz_mixin_prepatch.py` — a snapshot of
  `authz_mixin.py` from a clean Hermes checkout (no IMSG entries)
- `tests/fixtures/authz_mixin_postpatch.py` — the same file with the
  IMSG entries applied

If you ever regenerate the patch, regenerate BOTH fixtures from the
same source:

```bash
# 1. Get the pre-patch state (vanilla Hermes, no IMSG):
cp /path/to/vanilla/hermes-agent/gateway/authz_mixin.py \
   tests/fixtures/authz_mixin_prepatch.py

# 2. Apply your patch and save the result:
patch -p1 -d /path/to/vanilla/hermes-agent < patches/authz.patch
cp /path/to/vanilla/hermes-agent/gateway/authz_mixin.py \
   tests/fixtures/authz_mixin_postpatch.py
```

## The Unicode rendering pipeline

The `render_imessage()` function is a separate concern from the
subprocess plumbing. Its tests (`TestUnicodeRendering`) verify:

1. **Bold** `**bold**` → `𝗯𝗼𝗹𝗱` (Math Sans-Serif Bold, U+1D5D4..U+1D7F5)
2. **Italic** `*italic*` → `𝘪𝘵𝘢𝘭𝘪𝘤` (Math Sans-Serif Italic, U+1D608..U+1D63B)
3. **Code** `` `code` `` → `𝚌𝚘𝚍𝚎` (Math Monospace, U+1D670..U+1D7FF)
4. **Strike** `~~strike~~` → `s̶t̶r̶i̶k̶e̶` (U+0336 COMBINING LONG STROKE)
5. **Link** `[label](url)` → `label (url)` (iMessage has no inline link rendering)
6. **Snake_case** `foo_bar` → unchanged (italic-underscore regex requires word boundaries)
7. **Code blocks** ` ```code``` ` → contents rendered as monospace but asterisks preserved
8. **Emoji** `🦊` → preserved (multi-codepoint passthrough)

## End-to-end live smoke test (operator's Mac)

There's a one-off smoke test that exercises the adapter against a real
Hermes install — running the actual `imsg rpc` subprocess to send a
real iMessage. The script lives at `tests/live_smoke.py` (not part of
the pytest run, since it requires macOS + Full Disk Access + a signed-in
Messages.app).

Run it with:

```bash
/Users/soup/.hermes/hermes-agent/venv/bin/python tests/live_smoke.py
```

(Or with any Python that has `~/.hermes/hermes-agent` on `sys.path`.)
Set `IMSG_SMOKE_TARGET` to the E.164 phone or Apple ID email you want
to receive the test message.

## CI

`.github/workflows/test.yml` runs the pytest suite on every push and PR
against `macos-latest`. The CI runner has Python 3.11 and installs
`imsg` via Homebrew. The full suite runs — no `SKIP_LIVE_TESTS`
required.

See `.github/workflows/test.yml` for details.
