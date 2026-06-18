<!-- Improved compatibility of back to top link -->
<a id="readme-top"></a>

<!-- PROJECT SHIELDS -->
[![Tests][tests-shield]][tests-url]
[![Contributors][contributors-shield]][contributors-url]
[![Forks][forks-shield]][forks-url]
[![Stargazers][stars-shield]][stars-url]
[![Issues][issues-shield]][issues-url]
[![MIT License][license-shield]][license-url]
[![macOS][macos-shield]][macos-url]

<!-- PROJECT HEADER -->
<br />
<div align="center">
  <a href="https://github.com/ClawPlexDFW/hermes-imessage-adapter">
    <img src="assets/logo.png" alt="hermes-imessage-adapter" width="200" height="200">
  </a>

  <h3 align="center">hermes-imessage-adapter</h3>

  <p align="center">
    Native iMessage channel for Hermes Agent on macOS.
    <br />
    Drop one file in, patch one auth map, brew one CLI — and your Mac mini becomes an iMessage agent.
    <br />
    <a href="https://github.com/ClawPlexDFW/hermes-imessage-adapter"><strong>Explore the docs »</strong></a>
    <br />
    <br />
    <a href="https://github.com/ClawPlexDFW/hermes-imessage-adapter/issues/new?labels=bug&template=bug-report---.md">Report Bug</a>
    ·
    <a href="https://github.com/ClawPlexDFW/hermes-imessage-adapter/issues/new?labels=enhancement&template=feature-request---.md">Request Feature</a>
  </p>
</div>

## 🤖 For AI Agents — One-Shot Install Prompt

> **If you're an AI agent whose user just asked you to install this**, copy
> the block below and run it as your plan. The install is end-to-end and
> the verification step at the end confirms tool access is working.
>
> ```
> Install and verify the hermes-imessage-adapter so my user can message
> me via iMessage with full tool access. Do all of the following in order:
>
> 1. Run `brew install steipete/tap/imsg` and confirm version >= 0.11.1
>    with `imsg --version`. If older, upgrade with `brew upgrade imsg`.
> 2. Grant Full Disk Access to the running Python interpreter
>    (System Settings → Privacy & Security → Full Disk Access). The
>    Hermes gateway needs this to read `~/Library/Messages/chat.db`.
> 3. Clone this repo to a fresh directory and apply the three required
>    steps from the install section of README.md:
>    (a) `patch -p1 --forward --batch < patches/authz.patch` against the
>        live Hermes checkout
>    (b) copy `platforms/imsg.py` to the live `gateway/platforms/`
>    (c) copy `plugins/imsg-tools/` to `~/.hermes/plugins/`
> 4. Add the four YAML blocks to `~/.hermes/config.yaml` (see "Required
>    config" in the README): top-level `imsg:` block, `platform_toolsets
>    .imsg:` block, and `imsg-tools` in the `plugins.enabled` list.
> 5. Add `IMSG_ALLOW_ALL_USERS=true` to `~/.hermes/.env` (or
>    `IMSG_ALLOWED_USERS=+1...` for an explicit phone allowlist).
> 6. Restart the Hermes gateway and watch the log for
>    `✓ imsg connected`. If you see a DB permission error, the Full Disk
>    Access grant didn't take — re-grant and restart.
> 7. Verify the install worked by sending an iMessage to the user's
>    phone and asking a question that requires a tool call (e.g.
>    "search the web for today's weather in Dallas"). The user should
>    receive TWO messages back: a brief answer, then a 2nd message
>    containing the tool's output. If only one message arrives, the
>    imsg session is missing tools — re-check step 4.
> ```
>
> Report `done` when the verification round-trip succeeds, or the exact
> failure with the log line if it doesn't.


<!-- TABLE OF CONTENTS -->
<details>
  <summary>Table of Contents</summary>
  <ol>
    <li>
      <a href="#about-the-project">About The Project</a>
      <ul>
        <li><a href="#features">Features</a></li>
        <li><a href="#built-with">Built With</a></li>
      </ul>
    </li>
    <li>
      <a href="#getting-started">Getting Started</a>
      <ul>
        <li><a href="#prerequisites">Prerequisites</a></li>
        <li><a href="#installation">Installation</a></li>
      </ul>
    </li>
    <li><a href="#usage">Usage</a></li>
    <li><a href="#markdown-formatting">Markdown Formatting</a></li>
    <li><a href="#troubleshooting">Troubleshooting</a></li>
    <li><a href="#roadmap">Roadmap</a></li>
    <li><a href="#contributing">Contributing</a></li>
    <li><a href="#license">License</a></li>
    <li><a href="#contact">Contact</a></li>
    <li><a href="#acknowledgments">Acknowledgments</a></li>
  </ol>
</details>


<!-- ABOUT THE PROJECT -->
## About The Project

Hermes Agent ships a messaging gateway for ~20 platforms (Telegram, Discord,
Slack, Signal, …) — but not iMessage. This repo adds a native iMessage
channel that talks to your existing Messages.app via the
[`imsg`](https://github.com/steipete/imsg) CLI.

It's built for the rest of us: people with Mac minis who already have
Messages.app signed in and never wired it up to anything agent-shaped. No
BlueBubbles server, no cloud relay, no SMS gateway — just your local Apple
ID.

**Why this matters:** iMessage is end-to-end encrypted and tied to your
hardware. The agent only ever speaks through the same Messages.app you do.
Nothing leaves the machine except the message text and the standard Apple
push.

### Features

- **Inbound:** `imsg watch --json --reactions --attachments` streams new
  iMessages as NDJSON; the adapter turns each line into a `MessageEvent`
  for the gateway.
- **Outbound text:** `imsg rpc` over JSON-RPC 2.0 sends replies through
  Messages.app via AppleScript.
- **Outbound attachments:** `imsg send --file` sends images, files, and
  other media (with optional caption sent first as a text bubble).
- **Outbound tapback reactions:** `imsg react` sends ❤️ 👍 👎 😂 ‼️ ❓
  reactions on the most recent message in a chat.
- **Typing indicator:** `imsg typing` shows the typing bubble for ~8s so
  the human on the other end knows the agent is composing. **Note:** on
  macOS 26 (Tahoe) this requires SIP to be disabled (`csrutil disable` in
  Recovery Mode) because Apple restricts `imagent` (the Messages daemon)
  to Apple-private entitlements. The adapter detects this once and skips
  typing on macOS 26 + SIP enabled, logging a single warning — your
  messages still deliver, just without the typing bubble.
- **Group chat routing with opt-in allowlist:** outbound messages can be
  routed by chat_id (numeric rowid) for group threads; inbound `is_group: true`
  events are tagged with `chat_type="group"` and gated by the
  `allowed_group_ids` config — **groups are opt-in** (empty allowlist = ALL
  group messages dropped, preventing accidental broadcast to every group
  thread that pings the agent).
- **iMessage-flavored Unicode rendering:** `**bold**` → `𝗯𝗼𝗹𝗱`,
  `*italic*` → `𝘪𝘵𝘢𝘭𝘪𝘤`, `` `code` `` → `𝚌𝚘𝚍𝚎`, `~~strike~~` → `s̶t̶r̶i̶k̶e̶`.
  iOS doesn't render Markdown syntax; this converts it to the
  Math Sans-Serif / Math Mono / combining-strike Unicode characters
  Apple's font stack renders natively.
- **Self-loop guard:** filters out `is_from_me: true` events so the agent
  never responds to its own replies.
- **95-test pytest suite** covering helpers, JSON parsing, RPC, file send,
  reaction, typing, group allowlist, markdown rendering, patch idempotency,
  and the full install recipe.

<p align="right">(<a href="#readme-top">back to top</a>)</p>


### Built With

- [Python 3.11+](https://www.python.org/) — the adapter
- [imsg CLI](https://github.com/steipete/imsg) — the upstream Swift tool
  that reads/writes the iMessage database (`brew install steipete/tap/imsg`)
- [Hermes Agent gateway](https://github.com/hermes-agent) — the
  `BasePlatformAdapter` ABC this implements

<p align="right">(<a href="#readme-top">back to top</a>)</p>


<!-- GETTING STARTED -->
## Getting Started

Follow these steps in order. The whole thing takes about 5 minutes on a
Mac mini with Messages.app already signed in.

### Prerequisites

- **macOS 12+** with Messages.app signed in to your Apple ID
- **[Hermes Agent](https://github.com/hermes-agent)** installed (any
  recent version with the `gateway/platforms/` plugin directory)
- **`imsg` CLI:**
  ```sh
  brew install steipete/tap/imsg
  ```
- **Full Disk Access** for the Python binary running the Hermes gateway:
  `System Settings → Privacy & Security → Full Disk Access` and add the
  binary from `ps aux | grep gateway` (typically
  `~/.hermes/hermes-agent/venv/bin/python`). Without FDA, `imsg watch` will
  fail to read `~/Library/Messages/chat.db` with
  `authorization denied`.
- **Accessibility permission** (only required if you want tapback
  reactions): the same Python binary needs Accessibility so osascript can
  drive Messages.app for `imsg react`.

### Installation

> **⚠️ The imsg platform has 3 install steps that are easy to miss.**
> If you skip any of them, the adapter will connect but the agent will
> have an empty tool schema (no terminal, no web search, no file access,
> no reactions). All three are required:
>
> 1. **Core patch** — registers the `hermes-imsg` toolset in Hermes core
> 2. **Plugin install** — exposes `imsg_react` and `imsg_send_file` to the LLM
> 3. **Platform entry in `config.yaml`** — binds the toolset to the `imsg` platform
>
> Steps 2 (authz) and 3 (drop in the adapter file) are obvious. Steps 1
> and 2 above are the silent killers — the agent connects fine, but it
> has no tools. **Don't skip them.**

1. **Clone the repo**
   ```sh
   git clone https://github.com/ClawPlexDFW/hermes-imessage-adapter.git
   cd hermes-imessage-adapter
   ```

2. **Drop in the adapter.** Copy `platforms/imsg.py` into your Hermes
   install:
   ```sh
   cp platforms/imsg.py ~/.hermes/hermes-agent/gateway/platforms/imsg.py
   ```

3. **Patch the authz map.** From your Hermes repo root (where `gateway/`
   lives), apply the bundled patch with `--forward --batch` so the install
   is deterministic (refuses to re-apply if you've already run it):
   ```sh
   patch -p1 --forward --batch < /path/to/hermes-imessage-adapter/patches/authz.patch
   ```
   This adds `IMSG_ALLOWED_USERS` and `IMSG_ALLOW_ALL_USERS` to the
   existing platform allowlist maps in `gateway/authz_mixin.py`, mirroring
   how Telegram/Discord/etc. are configured.

4. **🛠 REQUIRED — patch the toolsets registry.** Apply the second bundled
   patch so the `hermes-imsg` toolset exists and is included in the
   `hermes-gateway` umbrella. **Without this, the imsg session has no
   tool calls** (the agent sees an empty tool schema):
   ```sh
   patch -p1 --forward --batch < /path/to/hermes-imessage-adapter/patches/hermes-core-toolsets.patch
   ```
   This adds a `hermes-imsg` toolset (full core tools + `imsg_react` /
   `imsg_send_file` from the imsg-tools plugin) and includes it in
   `hermes-gateway` alongside Telegram, Discord, etc.

5. **Choose your access policy.** In `~/.hermes/.env`, set **one of**:
   ```sh
   # Open access — anyone who iMessages you can chat the agent.
   # Recommended for single-user setups (iMessage DMs are 1:1 by design).
   IMSG_ALLOW_ALL_USERS=true

   # OR allowlist by phone number / email handle (comma-separated):
   IMSG_ALLOWED_USERS=+155****0100,jane@example.com
   ```

6. **🛠 REQUIRED — enable the platform** in `~/.hermes/config.yaml` with
   **both** blocks (the platform config AND the `platform_toolsets`
   entry — the second one is what gives the imsg agent its tools):
   ```yaml
   # Top-level platform block
   imsg:
     enabled: true
     # optional: defaults to /opt/homebrew/bin/imsg then /usr/local/bin/imsg
     cli_path: /opt/homebrew/bin/imsg
     # optional: pass --reactions to imsg watch (default: true)
     enable_reactions: true
     # optional: pass --attachments to imsg watch (default: true)
     enable_attachments: true
     # optional: convert outbound **bold** / *italic* / etc. to
     # iMessage-flavored Unicode. Default: true.
     unicode_format: true
     # optional: restrict to a single chat id (numeric rowid)
     chat_id: ""

   # Bind the full toolset surface to the imsg platform. This is
   # separate from the block above. Without this `imsg:` entry, the
   # agent connects but has no tools.
   platform_toolsets:
     # ... your existing platforms (telegram, discord, etc.) ...
     imsg:
     - browser
     - clarify
     - code_execution
     - computer_use
     - cronjob
     - delegation
     - file
     - image_gen
     - memory
     - messaging
     - session_search
     - skills
     - terminal
     - todo
     - tts
     - vision
     - web
     - imsg-tools
   ```
   The list above mirrors what telegram/discord ship with by default.
   Drop a toolset you don't want, or add others (`hermes-<name>` from
   your own plugins).

7. **🛠 REQUIRED — install the imsg-tools plugin** so the agent can call
   `imsg_react` (send tapback reactions) and `imsg_send_file` (send
   file attachments):
   ```sh
   # Copy the plugin into your local Hermes plugins directory
   cp -R plugins/imsg-tools ~/.hermes/plugins/

   # Enable it in config.yaml under the `plugins:` section
   # plugins:
   #   enabled:
   #   - disk-cleanup
   #   - imsg-tools       # <-- add this line
   ```
   Without this, the agent can send text and images but cannot react
   to incoming messages or attach files from disk.

8. **Restart the gateway**
   ```sh
   hermes gateway restart
   ```

#### Verify the install worked

After restarting, send a message to your iMessage thread and ask the
agent something that requires a tool — e.g. "what time is it" or
"search the web for X". The agent should make a tool call (you'll see
`api_calls=2+` in `~/.hermes/logs/gateway.log` for the response). If
you only see `api_calls=1`, the agent has no tools and you skipped
one of the steps marked 🛠 above.

You can also verify the toolset is loaded by running:
```sh
curl -s http://localhost:8765/v1/toolsets | python3 -m json.tool | grep imsg
```
(assuming the gateway API server is enabled; this is the same
endpoint used by `hermes tools`).


<p align="right">(<a href="#readme-top">back to top</a>)</p>


<!-- USAGE -->
## Usage

From your Messages.app, send the bot anything ("hello"). Within a few
seconds you should see the response land in the same thread.

Watch the live log to confirm the round trip:
```sh
tail -f ~/.hermes/logs/gateway.log | grep imsg
```

You should see:
```
✓ imsg connected
ImsgAdapter: watch subprocess started (pid=...)
inbound message: platform=imsg user=+155****0100 chat=+155****0100 msg='hello'
response ready: platform=imsg chat=+155****0100 time=4.8s
```

Any reply your agent produces shows up as a regular bubble in Messages.app,
on the same thread, from your Apple ID — indistinguishable from a message
you sent yourself (because Messages.app is what sent it).

**Group chats:** add the agent to a group text (from the iOS or macOS
Messages app) and inbound messages from that group will arrive with
`chat_type: "group"`. To reply to a group chat, use the chat's numeric
rowid (visible via `imsg chats --json`) as the `chat_id`. The adapter
auto-routes numeric IDs to `--chat-id` and handle/email-style IDs to
`--to`.

> ⚠️ **Group chats are opt-in.** By default, `allowed_group_ids` is empty
> and the adapter drops every group message before the agent sees it.
> This is intentional — it prevents the agent from accidentally
> broadcasting to every group thread that pings it. To allow a specific
> group, set its numeric rowid in the `imsg:` config block:
>
> ```yaml
> imsg:
>   enabled: true
>   allowed_group_ids: "7,8,42"   # comma-separated chat rowids
> ```
>
> You can also match by `chat_identifier` (e.g. `iMessage;+;abc123group`)
> if the rowid isn't available. DMs are never gated by this list.

**Reactions:** the agent can send a tapback on the most recent message in
a chat via `react(chat_id="<rowid>", message_id="...", reaction="love")`.
Valid named reactions: `love`, `like`, `dislike`, `laugh`, `emphasis`,
`question`. Custom emoji also work (iOS 17+ / macOS 14+). Note that
`imsg react` only acts on the **most recent** message in the thread — it
ignores the `message_id` argument, which is kept for API compatibility
with the base class.

**Typing indicators:** the agent can pulse the typing bubble for ~8s
before sending a long reply. Hermes's agent loop calls
`send_typing(chat_id)` automatically when generating — no extra config
needed.

**Attachments:** use `send_file(chat_id, file_path, caption=None)` to
send any local file. If `caption` is provided, the caption is sent as a
text bubble first and the file is sent as a follow-up attachment in the
same thread (iMessage's "send file + caption" pattern). The image
shortcut `send_image_file()` is also wired up.

<p align="right">(<a href="#readme-top">back to top</a>)</p>


<!-- MARKDOWN FORMATTING -->
## Markdown Formatting

iOS Messages.app **does not render Markdown syntax** — it shows literal
asterisks, underscores, and backticks. So `**bold**` appears as the text
`**bold**`, not as **bold**.

The adapter's `render_imessage()` converts common Markdown tokens to
iMessage-flavored Unicode before sending. The conversion uses the
**Mathematical Alphanumeric Symbols** range (U+1D400–U+1D7FF) which iOS's
font stack renders natively, with no extra fonts required.

| Markdown | iMessage Unicode | What you'll see |
|---|---|---|
| `**bold**` or `__bold__` | Math Sans-Serif Bold | 𝗯𝗼𝗹𝗱 |
| `*italic*` or `_italic_` | Math Sans-Serif Italic | 𝘪𝘵𝘢𝘭𝘪𝘤 |
| `` `code` `` or `` ```code``` `` | Math Monospace | 𝚌𝚘𝚍𝚎 |
| `~~strike~~` | COMBINING LONG STROKE OVERLAY | s̶t̶r̶i̶k̶e̶ |
| `[label](url)` | `label (url)` | label (url) |

Snake_case identifiers (`foo_bar_baz`) are **not** misread as italic —
the italic-underscore regex requires word boundaries.

To disable, set `unicode_format: false` in your `imsg:` config block.
Code blocks (triple-backtick spans) are protected — the contents are
rendered as monospace but not re-rendered as nested markdown.

<p align="right">(<a href="#readme-top">back to top</a>)</p>


<!-- TROUBLESHOOTING -->
## Troubleshooting

**`unable to open database chat.db: authorization denied`**
The gateway's Python binary doesn't have Full Disk Access. Find the path
with `ps aux | grep gateway`, then grant FDA in
`System Settings → Privacy & Security → Full Disk Access`. Restart the
gateway.

**`Invalid params: invalid service`**
You're on an old adapter version. The current `platforms/imsg.py` ships
the fix (`service: "imessage"` lowercase — `imsg`'s `MessageService` enum
is case-sensitive). Pull the latest.

**Inbound messages are delayed or missing**
`imsg watch` is event-driven on `chat.db` writes (FSEvents). A fresh
outbound iMessage from anywhere — even to yourself — will trigger any
queued events to flush. This is upstream behavior, not the adapter.

**The agent is replying to its own messages (loop)**
You're running an old adapter that didn't filter `is_from_me`. The
current `platforms/imsg.py` filters both `is_from_me` (snake_case, what
`imsg watch` emits) and `isFromMe` (camelCase, older versions) — keep
this file current.

**Tapback reactions fail with `osascript is not allowed to send keystrokes`**
The Hermes gateway's Python binary needs Accessibility permission
(different from Full Disk Access). Open
`System Settings → Privacy & Security → Accessibility` and add
`~/.hermes/hermes-agent/venv/bin/python` (or whichever binary `ps aux |
grep gateway` shows). Restart the gateway. Reactions that don't need
osascript — like text replies, file sends, and typing indicators — will
keep working without this permission.

**Bold/italic shows up as literal asterisks in the bubble**
The agent is sending `**text**` without Unicode rendering. Check that
`unicode_format: true` in your `imsg:` config block (it's the default).
If you explicitly set it to `false`, the adapter will pass Markdown
through unchanged.

**Test the install:**
```sh
cd /path/to/hermes-imessage-adapter
uv run --with pytest --with aiohttp python -m pytest tests/ -v
```
You should see 72 tests pass. The live-install tests
(`TestAuthzPatch`, `TestInstallDryRun`) auto-skip on CI via
`SKIP_LIVE_TESTS=1`.

<p align="right">(<a href="#readme-top">back to top</a>)</p>


<!-- ROADMAP -->
## Roadmap

- [x] Inbound NDJSON streaming
- [x] Outbound JSON-RPC 2.0 send
- [x] Self-loop guard (`is_from_me` filter)
- [x] Auth allowlist via `IMSG_ALLOWED_USERS` / `IMSG_ALLOW_ALL_USERS`
- [x] Tapback / reaction support (`--reactions` flag + `imsg react`)
- [x] Attachment send (`imsg send --file`)
- [x] Typing indicator (`imsg typing`)
- [x] Group chat routing (numeric chat_id + is_group flag)
- [x] Group chat opt-in allowlist (`allowed_group_ids` config)
- [x] iMessage-flavored Unicode rendering (bold / italic / mono / strike / link)
- [x] macOS 26 + SIP-aware typing fallback (skip when typing is impossible)
- [x] `imsg-tools` plugin exposing `imsg_react` + `imsg_send_file` agent tools
- [x] 95-test pytest suite covering all features (CI runs full suite — hermes-agent-independent via `tests/_stubs/`)

- [x] GitHub Actions CI on macos-latest
- [x] Live smoke test against a real Hermes install
- [ ] PR upstream to `hermes-agent/hermes` so this becomes a built-in

See the [open issues](https://github.com/ClawPlexDFW/hermes-imessage-adapter/issues)
for a full list of proposed features (and known issues).

<p align="right">(<a href="#readme-top">back to top</a>)</p>


<!-- CONTRIBUTING -->
## Contributing

PRs welcome. If you find a bug or want a feature, open an issue first so we
can talk about the design — Hermes's prompt-cache and config.yaml
stability rules are real, and adapter changes need to honor them.

1. Fork the repo
2. Create your feature branch (`git checkout -b feat/typing-indicator`)
3. Commit your changes (`git commit -m 'feat: add typing indicator'`)
4. Push to the branch (`git push origin feat/typing-indicator`)
5. Open a Pull Request

Before opening a PR:
```sh
uvx ruff check . && uvx ruff format --check . && \
  uv run --with pytest --with aiohttp python -m pytest tests/ -v
```

All three must pass. CI runs the same on macos-latest.

<p align="right">(<a href="#readme-top">back to top</a>)</p>


<!-- LICENSE -->
## License

Distributed under the MIT License. See `LICENSE` for the full text.

<p align="right">(<a href="#readme-top">back to top</a>)</p>


<!-- CONTACT -->
## Contact

Tyler Delano — [@tylerdotai](https://x.com/tylerdotai) —
tyler.delano@icloud.com

Project: [github.com/ClawPlexDFW/hermes-imessage-adapter](https://github.com/ClawPlexDFW/hermes-imessage-adapter)

DFW AI builder community: [clawplex.dev](https://clawplex.dev)

<p align="right">(<a href="#readme-top">back to top</a>)</p>


<!-- ACKNOWLEDGMENTS -->
## Acknowledgments

- [steipete/imsg](https://github.com/steipete/imsg) — the upstream Swift
  tool that reads/writes the iMessage database. The adapter is a thin
  Python shim around its CLI.
- [othneildrew/Best-README-Template](https://github.com/othneildrew/Best-README-Template)
  — the README structure you're reading right now.
- The [Hermes Agent](https://github.com/hermes-agent) team — for the
  `BasePlatformAdapter` ABC and the `authz_mixin` patch surface that
  makes this a 3-file install.

See [TESTING.md](TESTING.md) for how the test suite is structured and
how to run it locally.

<p align="right">(<a href="#readme-top">back to top</a>)</p>


<!-- MARKDOWN LINKS & IMAGES -->
[tests-shield]: https://img.shields.io/github/actions/workflow/status/ClawPlexDFW/hermes-imessage-adapter/test.yml?style=for-the-badge&label=tests
[tests-url]: https://github.com/ClawPlexDFW/hermes-imessage-adapter/actions/workflows/test.yml
[contributors-shield]: https://img.shields.io/github/contributors/ClawPlexDFW/hermes-imessage-adapter.svg?style=for-the-badge
[contributors-url]: https://github.com/ClawPlexDFW/hermes-imessage-adapter/graphs/contributors
[forks-shield]: https://img.shields.io/github/forks/ClawPlexDFW/hermes-imessage-adapter.svg?style=for-the-badge
[forks-url]: https://github.com/ClawPlexDFW/hermes-imessage-adapter/network/members
[stars-shield]: https://img.shields.io/github/stars/ClawPlexDFW/hermes-imessage-adapter.svg?style=for-the-badge
[stars-url]: https://github.com/ClawPlexDFW/hermes-imessage-adapter/stargazers
[issues-shield]: https://img.shields.io/github/issues/ClawPlexDFW/hermes-imessage-adapter.svg?style=for-the-badge
[issues-url]: https://github.com/ClawPlexDFW/hermes-imessage-adapter/issues
[license-shield]: https://img.shields.io/github/license/ClawPlexDFW/hermes-imessage-adapter.svg?style=for-the-badge
[license-url]: https://github.com/ClawPlexDFW/hermes-imessage-adapter/blob/main/LICENSE
[macos-shield]: https://img.shields.io/badge/macOS-12%2B-blue?style=for-the-badge&logo=apple
[macos-url]: https://www.apple.com/macos/
