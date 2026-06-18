"""iMessage platform adapter using the imsg CLI.

Uses the local imsg CLI for:
- Inbound:  ``imsg watch --json`` streams new messages as NDJSON lines
- Outbound: ``imsg rpc`` speaks JSON-RPC 2.0 over stdio

Requires:
  - macOS with Messages.app signed in
  - Full Disk Access granted to the Python binary running Hermes
  - ``imsg`` CLI installed (brew install steipete/tap/imsg)

Configuration in config.yaml::

    platforms:
      imsg:
        enabled: true
        # optional, defaults to ~/Library/Messages/chat.db
        db_path: ~/Library/Messages/chat.db
        # optional, path to imsg binary (auto-discovered if omitted)
        cli_path: /usr/local/bin/imsg
        # optional, restrict inbound to a single chat ID
        chat_id: ""
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import subprocess
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Set

from gateway.config import Platform, PlatformConfig
from gateway.platforms.base import (
    BasePlatformAdapter,
    MessageEvent,
    MessageType,
    SendResult,
)

logger = logging.getLogger(__name__)

MAX_MESSAGE_LENGTH = 4000  # iMessage soft limit

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

# Redact phone numbers / emails from log output
_PHONE_RE = re.compile(r"\+?\d{7,15}")
_EMAIL_RE = re.compile(r"[\w.+-]+@[\w.-]+\.[\w.]+")


def _redact(text: str) -> str:
    text = _PHONE_RE.sub("[REDACTED]", text)
    text = _EMAIL_RE.sub("[REDACTED]", text)
    return text


def _normalize_phone(raw: str) -> str:
    """Return a stripped, +-prefixed E.164 phone number or empty string."""
    if not raw:
        return ""
    # Strip whitespace and common separators
    cleaned = re.sub(r"[\s\-().]", "", raw)
    if not cleaned:
        return ""
    if not cleaned.startswith("+"):
        cleaned = "+" + cleaned
    return cleaned


def check_imsg_requirements() -> bool:
    """Return True when imsg is available on PATH (or configured cli_path)."""
    try:
        result = subprocess.run(
            ["imsg", "--version"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        return result.returncode == 0
    except Exception:
        return False


def _discover_imsg_path() -> str:
    """Return the imsg binary path, checking common locations."""
    candidates = [
        "/usr/local/bin/imsg",
        "/opt/homebrew/bin/imsg",
        "/opt/local/bin/imsg",
    ]
    for path in candidates:
        if os.path.isfile(path) and os.access(path, os.X_OK):
            return path
    return "imsg"  # fallback to PATH lookup


# ---------------------------------------------------------------------------
# Adapter
# ---------------------------------------------------------------------------

class ImsgAdapter(BasePlatformAdapter):
    """iMessage adapter using the imsg CLI subprocess."""

    platform = Platform.IMSG
    SUPPORTS_MESSAGE_EDITING = False
    MAX_MESSAGE_LENGTH = MAX_MESSAGE_LENGTH

    # iMessage has no @mention semantics — these wake-word patterns are
    # matched against message text to decide whether to process a message.
    DEFAULT_MENTION_PATTERNS = [
        r"(?<!\w)hermes(?:\s+agent)?[,\s:\-]?",
        r"(?<!\w)hoss[,\s:\-]?",
    ]

    def __init__(self, config: PlatformConfig):
        super().__init__(config, Platform.IMSG)

        extra = config.extra or {}

        self.db_path: str = os.path.expanduser(
            extra.get("db_path", "~/Library/Messages/chat.db")
        )
        self.cli_path: str = os.path.expanduser(
            extra.get("cli_path", _discover_imsg_path())
        )
        # Optional: restrict inbound/outbound to one chat
        self.chat_id: str = extra.get("chat_id", "")

        self._proc: Optional[asyncio.subprocess.Process] = None
        self._reader_task: Optional[asyncio.Task] = None
        self._running = False

        # Track rowids we've already emitted to avoid duplicates on reconnect
        self._seen_rowids: Set[str] = set()
        self._max_seen_rowids = 1000

        logger.info(
            "ImsgAdapter: cli=%s db=%s",
            self.cli_path,
            _redact(self.db_path),
        )

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def connect(self) -> bool:
        """Start ``imsg watch`` subprocess and stream handler."""
        if not self._acquire_platform_lock(
            "imsg", self.platform.value, "imsg adapter"
        ):
            return False

        self._running = True
        self._reader_task = asyncio.create_task(self._watch_loop())
        logger.info("ImsgAdapter: connected")
        return True

    async def disconnect(self) -> None:
        """Stop the watch subprocess."""
        self._running = False

        if self._reader_task:
            self._reader_task.cancel()
            try:
                await self._reader_task
            except asyncio.CancelledError:
                pass
            self._reader_task = None

        if self._proc:
            try:
                self._proc.terminate()
                await asyncio.wait_for(self._proc.wait(), timeout=5.0)
            except Exception:
                pass
            self._proc = None

        self._release_platform_lock()
        logger.info("ImsgAdapter: disconnected")

    # ------------------------------------------------------------------
    # Inbound: imsg watch (NDJSON stream)
    # ------------------------------------------------------------------

    async def _watch_loop(self) -> None:
        """Run ``imsg watch --json`` and emit MessageEvents for each line."""
        backoff = 2.0
        max_backoff = 60.0

        while self._running:
            try:
                cmd = [
                    self.cli_path,
                    "watch",
                    "--json",
                    "--debounce", "250ms",
                ]
                if self.chat_id:
                    cmd.extend(["--chat-id", self.chat_id])

                self._proc = await asyncio.create_subprocess_exec(
                    *cmd,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                    limit=65536,
                )

                logger.info(
                    "ImsgAdapter: watch subprocess started (pid=%s)",
                    self._proc.pid,
                )
                backoff = 2.0  # reset backoff on successful start

                # Drain stdout line by line
                if self._proc.stdout:
                    async for line in self._read_lines(self._proc.stdout):
                        if not self._running:
                            break
                        await self._handle_watch_line(line)

                # proc exited — check reason
                rc = await self._proc.wait()
                if self._running:
                    logger.warning(
                        "ImsgAdapter: watch exited with %s, restarting in %.0fs",
                        rc,
                        backoff,
                    )
                    await asyncio.sleep(backoff)
                    backoff = min(backoff * 2, max_backoff)

            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error("ImsgAdapter: watch loop error: %s", e)
                if self._running:
                    await asyncio.sleep(backoff)
                    backoff = min(backoff * 2, max_backoff)

    async def _read_lines(self, stream: asyncio.StreamReader):
        """Yield each NDJSON line from the stream as a string."""
        buffer = ""
        while True:
            try:
                chunk = await asyncio.wait_for(stream.read(4096), timeout=30.0)
                if not chunk:
                    break
                buffer += chunk.decode("utf-8", errors="replace")
                while "\n" in buffer:
                    line, buffer = buffer.split("\n", 1)
                    line = line.strip()
                    if line:
                        yield line
            except asyncio.TimeoutError:
                continue

    async def _handle_watch_line(self, line: str) -> None:
        """Parse one NDJSON line from imsg watch and emit a MessageEvent.

        JSON shape (from imsg's MessagePayload):
            id: int64                    -- message rowid
            chatID: int64                -- chat rowid
            guid: str
        # JSON shape (verified from `imsg history --json` and watch stream):
        #   guid, id, chat_id, created_at, sender (phone/email),
        #   is_from_me, text, chat_identifier, chat_guid, chat_name,
        #   destination_caller_id, is_group, participants, attachments
        # Older versions may use camelCase isFromMe; handle both.
            text: str
            createdAt: str               -- ISO8601
            chat_identifier: str
            chat_guid: str
            chat_name: str
            is_group: bool
            ...
        """
        try:
            msg = json.loads(line)
        except json.JSONDecodeError:
            return

        # Skip our own outgoing messages — only handle inbound (or replies in DMs)
        if msg.get("is_from_me") or msg.get("isFromMe"):
            return

        # Skip reactions and other non-message payloads (when present)
        # The default watch stream emits full messages, not reaction events,
        # unless --reactions is passed; we don't pass it.

        # Deduplicate by id
        rowid = str(msg.get("id") or "")
        if rowid and rowid in self._seen_rowids:
            return
        if rowid:
            self._seen_rowids.add(rowid)
            if len(self._seen_rowids) > self._max_seen_rowids:
                excess = len(self._seen_rowids) - self._max_seen_rowids
                for _ in range(excess):
                    self._seen_rowids.pop()

        # Extract sender (it's a flat string, not a dict)
        sender_handle = msg.get("sender") or ""
        sender_number = _normalize_phone(sender_handle)
        sender_name = msg.get("senderName") or ""

        # Chat ID: prefer the chat_identifier (phone/email), fallback to chatID
        chat_identifier = msg.get("chat_identifier") or ""
        chat_id = chat_identifier or str(msg.get("chatID") or sender_number or "")

        # Message body
        text = msg.get("text") or ""
        if not text:
            return  # attachment-only or empty message

        # Reply context
        reply_to_guid = msg.get("replyToGUID") or None
        reply_to_text = msg.get("replyToText") or None

        # Build MessageEvent
        event = MessageEvent(
            text=text,
            message_type=MessageType.TEXT,
            message_id=rowid or None,
            reply_to_message_id=reply_to_guid,
            reply_to_text=reply_to_text,
            source=self._make_source(
                chat_id=chat_id,
                sender_number=sender_number,
                sender_name=sender_name,
            ),
            raw_message=msg,
            timestamp=self._parse_timestamp(msg.get("createdAt")),
        )

        await self.handle_message(event)

    def _make_source(
        self, *, chat_id: str, sender_number: str, sender_name: str
    ):
        """Build a SessionSource for this inbound message."""
        from gateway.session import SessionSource

        return SessionSource(
            platform=self.platform,
            chat_id=chat_id,
            chat_name=sender_name or None,
            chat_type="dm",
            user_id=sender_number or None,
            user_name=sender_name or None,
            message_id=None,
            role_authorized=False,
        )

    def _parse_timestamp(self, value: Any) -> datetime:
        """Parse an ISO-8601 or unix timestamp into a datetime."""
        if isinstance(value, (int, float)):
            try:
                return datetime.fromtimestamp(value, tz=timezone.utc)
            except Exception:
                return datetime.now(tz=timezone.utc)
        if isinstance(value, str):
            try:
                if value.endswith("Z"):
                    value = value[:-1] + "+00:00"
                return datetime.fromisoformat(value)
            except Exception:
                pass
        return datetime.now(tz=timezone.utc)

    # ------------------------------------------------------------------
    # Outbound: imsg rpc (JSON-RPC 2.0 over stdio)
    # ------------------------------------------------------------------

    async def send(
        self,
        chat_id: str,
        content: str,
        reply_to: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> SendResult:
        """Send a text message via ``imsg rpc send``."""
        if not content:
            return SendResult(success=False, error="empty content")

        if len(content) > MAX_MESSAGE_LENGTH:
            content = content[: MAX_MESSAGE_LENGTH - 3] + "..."

        try:
            result = await self._rpc_call(
                "send",
                {
                    "to": chat_id,
                    "text": content,
                    "service": "imessage",
                },
            )
            message_id = str(result.get("id") or result.get("messageId") or "")
            return SendResult(success=True, message_id=message_id)
        except Exception as e:
            logger.error("ImsgAdapter send error: %s", e)
            return SendResult(success=False, error=str(e))

    async def _rpc_call(self, method: str, params: Dict[str, Any]) -> Dict[str, Any]:
        """Make a JSON-RPC 2.0 call over stdio to imsg rpc."""
        rpc_id = f"h{os.urandom(4).hex()}"
        request = {
            "jsonrpc": "2.0",
            "id": rpc_id,
            "method": method,
            "params": params,
        }

        proc = await asyncio.create_subprocess_exec(
            self.cli_path,
            "rpc",
            "--json",
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            limit=65536,
        )

        stdout_data, stderr_data = await proc.communicate(
            input=json.dumps(request).encode("utf-8")
        )

        if proc.returncode != 0:
            stderr = stderr_data.decode("utf-8", errors="replace").strip()
            raise RuntimeError(f"imsg rpc exited {proc.returncode}: {stderr}")

        try:
            response = json.loads(stdout_data.decode("utf-8", errors="replace"))
        except json.JSONDecodeError as e:
            raise RuntimeError(
                f"invalid JSON-RPC response: {e}\n{stdout_data!r}"
            )

        if response.get("error"):
            raise RuntimeError(f"JSON-RPC error: {response['error']}")

        return response.get("result") or {}

    # ------------------------------------------------------------------
    # Unsupported methods
    # ------------------------------------------------------------------

    async def send_typing(
        self, chat_id: str, metadata=None
    ) -> None:
        """iMessage has no typing indicator API via imsg."""
        pass

    async def mark_read(self, chat_id: str, message_id: str) -> bool:
        """Mark a message as read."""
        try:
            await self._rpc_call(
                "markRead", {"chatId": chat_id, "messageId": message_id}
            )
            return True
        except Exception:
            return False

    async def react(
        self, chat_id: str, message_id: str, reaction: str
    ) -> SendResult:
        """Send a tapback reaction."""
        try:
            await self._rpc_call(
                "react",
                {"chatId": chat_id, "messageId": message_id, "reaction": reaction},
            )
            return SendResult(success=True, message_id=message_id)
        except Exception as e:
            return SendResult(success=False, error=str(e))

    async def get_chat_info(self, chat_id: str) -> Dict[str, Any]:
        """Look up a chat by phone number / address via imsg chats."""
        try:
            # imsg chats returns all chats; find the matching one
            proc = await asyncio.create_subprocess_exec(
                self.cli_path,
                "chats",
                "--json",
                "--limit", "100",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                limit=65536,
            )
            stdout_data, _ = await proc.communicate()
            if proc.returncode != 0:
                return {"name": chat_id, "type": "dm"}

            chats = json.loads(stdout_data.decode("utf-8", errors="replace"))
            for chat in chats:
                if str(chat.get("chat_id") or "") == chat_id:
                    return {
                        "name": chat.get("contact_name") or chat_id,
                        "type": "dm",
                    }
            return {"name": chat_id, "type": "dm"}
        except Exception:
            return {"name": chat_id, "type": "dm"}
