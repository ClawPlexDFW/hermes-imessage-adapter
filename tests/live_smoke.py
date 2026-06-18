"""Live end-to-end smoke test for hermes-imessage-adapter.

Exercises the real adapter against a real Hermes install: imports
gateway.platforms.imsg, builds an ImsgAdapter, and uses its send()
method to dispatch a real JSON-RPC 2.0 call to `imsg rpc`. The
message will appear in the iMessage thread of whatever handle you
pass as chat_id.

Requires:
- macOS
- imsg CLI on PATH (brew install steipete/tap/imsg)
- Full Disk Access granted to the running Python
- Messages.app signed in to an Apple ID

Run with:
    /Users/soup/.hermes/hermes-agent/venv/bin/python tests/live_smoke.py
"""

from __future__ import annotations

import asyncio
import os
import sys
import time
from pathlib import Path

HERMES_ROOT = Path("/Users/soup/.hermes/hermes-agent")
if str(HERMES_ROOT) not in sys.path:
    sys.path.insert(0, str(HERMES_ROOT))

from gateway.config import PlatformConfig  # noqa: E402
from gateway.platforms import imsg  # noqa: E402

# IMPORTANT: set the IMSG_SMOKE_TARGET env var to the iMessage handle
# (E.164 phone number or Apple ID email) you want to receive the test
# message. No phone numbers are hard-coded in the repo so that this
# file is safe to share publicly.
TARGET_HANDLE = os.environ.get("IMSG_SMOKE_TARGET", "")

if not TARGET_HANDLE:
    print(
        "ERROR: IMSG_SMOKE_TARGET env var is not set. Export the E.164\n"
        "phone number or Apple ID email you want the smoke-test message\n"
        "delivered to, e.g.:\n"
        '  export IMSG_SMOKE_TARGET="+15551234567"\n'
        "Then re-run this script.",
        file=sys.stderr,
    )
    sys.exit(2)


async def main() -> int:
    print("=" * 60)
    print("LIVE SMOKE TEST — hermes-imessage-adapter")
    print("=" * 60)

    print(f"Platform.IMSG: {imsg.Platform.IMSG}")
    print(f"ImsgAdapter.platform: {imsg.ImsgAdapter.platform}")
    print(f"check_imsg_requirements(): {imsg.check_imsg_requirements()}")
    print(f"_discover_imsg_path(): {imsg._discover_imsg_path()}")
    print()

    cfg = PlatformConfig(enabled=True, extra={})
    adapter = imsg.ImsgAdapter(cfg)
    print(f"Adapter cli_path: {adapter.cli_path}")
    print()

    marker = f"[smoke-test] hoss is alive at {time.strftime('%H:%M:%S')}"
    print(f"Sending: {marker!r}")
    result = await adapter.send(chat_id=TARGET_HANDLE, content=marker)
    print(
        f"Result: success={result.success}, "
        f"message_id={result.message_id}, error={result.error}"
    )
    print()

    if result.success:
        print("✓ LIVE RPC SEND OK")
        print("  message should be visible in Messages.app now")
        print(f"  message_id: {result.message_id}")
        return 0
    else:
        print(f"✗ LIVE RPC SEND FAILED: {result.error}")
        return 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
