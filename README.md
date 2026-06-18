# hermes-imessage-adapter

Native iMessage channel for [Hermes Agent](https://github.com/hermes-agent) on macOS.

Drops a single platform adapter into your Hermes install so your agent can
send and receive iMessages via the `imsg` CLI. Works alongside your existing
Telegram/Discord/Slack channels.

## What you get

- **Inbound:** `imsg watch --json` streams new iMessages as NDJSON; the adapter
  turns each line into a `MessageEvent` for the gateway.
- **Outbound:** `imsg rpc` over JSON-RPC 2.0 sends replies through Messages.app.
- **Self-loop guard:** filters out `is_from_me: true` events so the agent never
  responds to its own replies.
- **DM-only by design:** iMessage is a 1:1 protocol, so the adapter does not
  handle group routing.

## Requirements

- macOS 12+ with **Messages.app** signed in to your Apple ID
- [Hermes Agent](https://github.com/hermes-agent) installed (any recent version
  with the `gateway/platforms/` plugin directory)
- `imsg` CLI:

  ```bash
  brew install steipete/tap/imsg
  ```

  The adapter calls `/opt/homebrew/bin/imsg` (or `/usr/local/bin/imsg` on
  Intel Macs) and falls back to `imsg` on `$PATH`.

- **Full Disk Access** for the Python binary running the Hermes gateway:
  `System Settings → Privacy & Security → Full Disk Access` and add the
  binary from `ps aux | grep gateway` (typically
  `~/.hermes/hermes-agent/venv/bin/python`). Without FDA, `imsg watch` will
  fail to read `~/Library/Messages/chat.db`.

## Install

### 1. Drop in the adapter

Copy `platforms/imsg.py` into your Hermes install:

```bash
cp platforms/imsg.py ~/.hermes/hermes-agent/gateway/platforms/imsg.py
```

### 2. Patch the authz map

Hermes's auth gate (`gateway/authz_mixin.py`) hard-codes the platform
allowlist env-var map and doesn't know about iMessage yet. Apply the bundled
patch from your Hermes repo root (where `gateway/` lives):

```bash
patch -p1 < patches/authz.patch
```

This adds `IMSG_ALLOWED_USERS` and `IMSG_ALLOW_ALL_USERS` to the existing
maps, mirroring how Telegram/Discord/etc. work.

### 3. Choose your access policy

In `~/.hermes/.env`, set **one of**:

```bash
# Open access — anyone who iMessages you can chat the agent.
# Recommended for single-user setups (iMessage DMs are 1:1 by design).
IMSG_ALLOW_ALL_USERS=true

# OR allowlist by phone number / email handle (comma-separated):
IMSG_ALLOWED_USERS=+15555550100,jane@example.com
```

### 4. Enable the platform in `config.yaml`

```yaml
imsg:
  enabled: true
  # optional: defaults to /opt/homebrew/bin/imsg then /usr/local/bin/imsg
  cli_path: /opt/homebrew/bin/imsg
```

### 5. Restart the gateway

```bash
hermes gateway restart
```

## Verify

From your Messages.app, send the bot anything ("hello"). Within a few seconds
you should see the response land in the same thread. Watch the live log:

```bash
tail -f ~/.hermes/logs/gateway.log | grep imsg
```

You should see:

```
✓ imsg connected
ImsgAdapter: watch subprocess started (pid=...)
inbound message: platform=imsg user=+15555550100 chat=+15555550100 msg='hello'
response ready: platform=imsg chat=+15555550100 time=4.8s
```

## Troubleshooting

**`unable to open database chat.db: authorization denied`**
The gateway's Python binary doesn't have Full Disk Access. Find the path with
`ps aux | grep gateway`, then grant FDA in
`System Settings → Privacy & Security → Full Disk Access`. Restart the gateway.

**`Invalid params: invalid service`**
You're on an old adapter version. This README and `platforms/imsg.py` already
ship the fix (`service: "imessage"` lowercase). Pull the latest.

**Inbound messages are delayed or missing**
`imsg watch` is event-driven on `chat.db` writes (FSEvents). A fresh
outbound iMessage from anywhere — even to yourself — will trigger any queued
events to flush. This is upstream behavior, not the adapter.

**The agent is replying to its own messages (loop)**
You're running an old adapter that didn't filter `is_from_me`. The current
`platforms/imsg.py` filters both `is_from_me` (snake_case, what `imsg watch`
emits) and `isFromMe` (camelCase, older versions) — keep this file current.

## Files in this repo

| File                          | What it is                                         |
| ----------------------------- | -------------------------------------------------- |
| `platforms/imsg.py`           | The adapter — drop into `gateway/platforms/`        |
| `patches/authz.patch`         | One-shot patch to `gateway/authz_mixin.py`          |
| `README.md`                   | This file                                          |
| `LICENSE`                     | MIT                                                |

## Credits

- `imsg` CLI by [steipete](https://github.com/steipete/imsg) — the upstream
  Swift tool that reads/writes the iMessage database.
- Hermes Agent gateway channel architecture by the Hermes team.

## License

MIT — see `LICENSE`.
