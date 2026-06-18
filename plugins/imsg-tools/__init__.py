"""imsg-tools plugin — agent-callable helpers for the iMessage platform.

Registers two tools on the `imsg` toolset:

- ``imsg_react``: send a tapback (love/like/laugh/etc.) to a chat.
- ``imsg_send_file``: send a file as an iMessage attachment.

Both are subprocess-driven (`imsg react`, `imsg send --file`) because the
``imsg rpc`` JSON-RPC interface only exposes the ``send`` method. The
platform adapter (gateway/platforms/imsg.py) handles inbound watch and
text sends; this plugin covers the **outbound** capabilities the
adapter exposes as methods but that no agent tool registered by default.

Install: drop this directory in ``~/.hermes/plugins/imsg-tools/`` and
add ``imsg-tools`` to ``plugins.enabled`` in ``config.yaml``. The
``imsg`` CLI itself is the only external dependency (brew install
steipete/tap/imsg).
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import shutil
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Subprocess helpers
# ---------------------------------------------------------------------------

def _imsg_cli_path() -> str:
    """Resolve the ``imsg`` binary. Honors ``$IMSG_CLI`` for tests."""
    override = os.environ.get("IMSG_CLI")
    if override:
        return override
    found = shutil.which("imsg")
    return found or "/opt/homebrew/bin/imsg"


async def _imsg_subprocess(*args: str, timeout: float = 8.0) -> Dict[str, Any]:
    """Run an ``imsg`` subcommand and return a structured result.

    Returns
    -------
    dict
        ``{"ok": bool, "stdout": str, "stderr": str, "returncode": int}``
    """
    cmd = [_imsg_cli_path(), *args]
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
    except FileNotFoundError as exc:
        return {
            "ok": False,
            "stdout": "",
            "stderr": f"imsg binary not found: {exc}",
            "returncode": -1,
        }
    try:
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        try:
            proc.kill()
        except Exception:
            pass
        return {
            "ok": False,
            "stdout": "",
            "stderr": f"imsg {args[0] if args else ''} timed out after {timeout}s",
            "returncode": -1,
        }
    out = stdout.decode(errors="replace").strip()
    err = stderr.decode(errors="replace").strip()
    return {
        "ok": proc.returncode == 0,
        "stdout": out,
        "stderr": err,
        "returncode": proc.returncode or 0,
    }


# ---------------------------------------------------------------------------
# Tool: imsg_react
# ---------------------------------------------------------------------------

IMSG_REACT_SCHEMA = {
    "name": "imsg_react",
    "description": (
        "Send a tapback reaction (love, like, dislike, laugh, emphasis, "
        "question) to the most recent message in an iMessage chat. "
        "Requires the `imsg` CLI on PATH (brew install steipete/tap/imsg) "
        "and Accessibility permission for Hermes in System Settings. "
        "Use this sparingly — only when a tapback genuinely fits the "
        "moment (a laugh at something funny, a heart at something sweet, "
        "a thumbs-up to acknowledge a quick yes/no). Do not react to "
        "every message; that comes across as performative."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "chat_id": {
                "type": "string",
                "description": (
                    "The chat to react in. Accepts the chat rowid (numeric "
                    "string), an iMessage chat identifier (e.g. "
                    "'iMessage;-;+141****1212'), or a phone number / email."
                ),
            },
            "reaction": {
                "type": "string",
                "enum": ["love", "like", "dislike", "laugh", "emphasis", "question"],
                "description": "The tapback reaction type.",
            },
        },
        "required": ["chat_id", "reaction"],
    },
}


async def _handle_imsg_react(args: Dict[str, Any], **kwargs) -> str:
    chat_id = str(args.get("chat_id") or "").strip()
    reaction = str(args.get("reaction") or "").strip().lower()
    if not chat_id:
        return json.dumps({"ok": False, "error": "chat_id is required"})
    if reaction not in {"love", "like", "dislike", "laugh", "emphasis", "question"}:
        return json.dumps({"ok": False, "error": f"unknown reaction: {reaction!r}"})

    # imsg react only accepts --chat-id (rowid integer) and --reaction.
    # No --to / --chat-identifier support — must resolve to rowid first.
    # If we got a non-numeric chat_id, try to look it up via the platform
    # adapter's module-level chat rowid cache (populated by inbound
    # `_handle_watch_line` calls).
    resolved = chat_id
    if not chat_id.isdigit():
        try:
            from gateway.platforms.imsg import CHAT_ROWID_BY_IDENTIFIER  # type: ignore
            rowid = CHAT_ROWID_BY_IDENTIFIER.get(chat_id)
        except Exception:
            rowid = None
        if rowid is not None:
            resolved = str(rowid)
        else:
            return json.dumps({
                "ok": False,
                "error": (
                    f"imsg_react requires a numeric chat_id (rowid); got "
                    f"{chat_id!r}. Wait for an inbound message in that chat "
                    f"to populate the rowid cache, or pass the integer rowid "
                    f"directly."
                ),
                "chat_id": chat_id,
            })

    cmd: List[str] = ["react", "--chat-id", resolved, "--reaction", reaction]

    result = await _imsg_subprocess(*cmd, timeout=8.0)
    if not result["ok"]:
        logger.warning(
            "imsg_react failed: rc=%d stderr=%s",
            result["returncode"], result["stderr"][:200],
        )
    return json.dumps(
        {
            "ok": result["ok"],
            "chat_id": chat_id,
            "reaction": reaction,
            "stderr": result["stderr"][:200],
        }
    )


# ---------------------------------------------------------------------------
# Tool: imsg_send_file
# ---------------------------------------------------------------------------

IMSG_SEND_FILE_SCHEMA = {
    "name": "imsg_send_file",
    "description": (
        "Send a file as an iMessage attachment to a chat. Supports any "
        "file type Messages.app accepts (images, videos, PDFs, audio, "
        "voice memos, etc.). The file is passed verbatim to `imsg send "
        "--file`; the CLI handles the AppleScript bridge to Messages.app. "
        "Use this when the user asks you to share a file, screenshot, or "
        "media — pair it with a short caption (also passed to --text) so "
        "the recipient knows what they're looking at."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "chat_id": {
                "type": "string",
                "description": (
                    "The chat to send to. Accepts the chat rowid, an "
                    "iMessage chat identifier, or a phone number / email."
                ),
            },
            "file_path": {
                "type": "string",
                "description": (
                    "Absolute path to the file on the local filesystem. "
                    "The file must exist and be readable by the Hermes "
                    "process."
                ),
            },
            "caption": {
                "type": "string",
                "description": (
                    "Optional text caption. Sent as the message body "
                    "alongside the attachment. Keep it short — iMessage "
                    "captions render below the file preview."
                ),
            },
        },
        "required": ["chat_id", "file_path"],
    },
}


async def _handle_imsg_send_file(args: Dict[str, Any], **kwargs) -> str:
    chat_id = str(args.get("chat_id") or "").strip()
    file_path = str(args.get("file_path") or "").strip()
    caption: Optional[str] = args.get("caption")

    if not chat_id:
        return json.dumps({"ok": False, "error": "chat_id is required"})
    if not file_path:
        return json.dumps({"ok": False, "error": "file_path is required"})

    # Expand ~ and resolve to absolute
    expanded = Path(file_path).expanduser().resolve()
    if not expanded.exists():
        return json.dumps({"ok": False, "error": f"file not found: {file_path}"})
    if not expanded.is_file():
        return json.dumps({"ok": False, "error": f"not a regular file: {file_path}"})

    cmd: List[str] = ["send", "--file", str(expanded)]
    # imsg send --to <contact> --file <path> [--text <caption>]
    if chat_id.isdigit():
        cmd.extend(["--chat-id", chat_id])
    elif chat_id.startswith("iMessage;-;") or chat_id.startswith("any;-;"):
        cmd.extend(["--chat-identifier", chat_id])
    else:
        cmd.extend(["--to", chat_id])
    if caption:
        cmd.extend(["--text", caption])

    result = await _imsg_subprocess(*cmd, timeout=15.0)
    if not result["ok"]:
        logger.warning(
            "imsg_send_file failed: rc=%d stderr=%s",
            result["returncode"], result["stderr"][:200],
        )
    return json.dumps(
        {
            "ok": result["ok"],
            "chat_id": chat_id,
            "file": str(expanded),
            "stderr": result["stderr"][:200],
        }
    )


# ---------------------------------------------------------------------------
# Plugin registration
# ---------------------------------------------------------------------------

_TOOLS = (
    ("imsg_react", IMSG_REACT_SCHEMA, _handle_imsg_react, "❤️"),
    ("imsg_send_file", IMSG_SEND_FILE_SCHEMA, _handle_imsg_send_file, "📎"),
)


def register(ctx) -> None:
    """Register both tools on the ``imsg`` toolset.

    Called once by the plugin loader during gateway startup. We do NOT
    bind a ``check_fn`` — the tools appear in the agent's schema
    regardless of whether ``imsg`` is installed, and the handler returns
    a structured error if the binary is missing. (This matches the
    behaviour of built-in tools that defer their preconditions to runtime.)
    """
    for name, schema, handler, emoji in _TOOLS:
        ctx.register_tool(
            name=name,
            toolset="imsg",
            schema=schema,
            handler=handler,
            is_async=True,
            description=schema["description"],
            emoji=emoji,
        )
    logger.info(
        "imsg-tools plugin registered: %s",
        ", ".join(name for name, *_ in _TOOLS),
    )
