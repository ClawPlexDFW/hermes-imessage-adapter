"""iMessage platform adapter using the imsg CLI.

Inbound:
    ``imsg watch --json --reactions --attachments`` streams events as NDJSON.
    Each line may be a regular message, an attachment-bearing message, or
    a tapback reaction event.

Outbound:
    - Text:    ``imsg rpc send``  (JSON-RPC 2.0 over stdio)
    - File:    ``imsg send --file``  (subprocess CLI; no rpc equivalent)
    - React:   ``imsg react``  (subprocess CLI; uses AppleScript — needs
               Accessibility permission for the Hermes process)
    - Typing:  ``imsg typing``  (subprocess CLI; one-shot start + stop)

Requires:
  - macOS with Messages.app signed in
  - Full Disk Access granted to the Python binary running Hermes
  - Accessibility permission (only required for ``react``)
  - ``imsg`` CLI installed (brew install steipete/tap/imsg)

Configuration in config.yaml::

    imsg:
      enabled: true
      # optional, defaults to ~/Library/Messages/chat.db
      db_path: ~/Library/Messages/chat.db
      # optional, path to imsg binary (auto-discovered if omitted)
      cli_path: /usr/local/bin/imsg
      # optional, restrict inbound to a single chat ID
      chat_id: ""
      # optional, listen for tapback reactions in --reactions mode
      enable_reactions: true
      # optional, include attachment metadata in --attachments mode
      enable_attachments: true
      # optional, format outbound text with iMessage-flavored Unicode
      # (bold/italic/underline/strikethrough/mono). Default true.
      unicode_format: true
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional, Set

from gateway.config import Platform, PlatformConfig
from gateway.platforms.base import (
    BasePlatformAdapter,
    MessageEvent,
    MessageType,
    SendResult,
)

logger = logging.getLogger(__name__)

# Module-level cache: chat_identifier -> rowid mapping, populated by
# `_handle_watch_line` on every inbound message. Exposed for use by external
# callers (e.g. the `imsg-tools` plugin's `imsg_react` handler) that need to
# resolve a non-numeric chat_id to the rowid required by `imsg react --chat-id`.
CHAT_ROWID_BY_IDENTIFIER: Dict[str, int] = {}

MAX_MESSAGE_LENGTH = 4000  # iMessage soft limit

# Valid reaction types accepted by `imsg react`
VALID_REACTIONS = {
    "love",
    "like",
    "dislike",
    "laugh",
    "emphasis",
    "question",
}

# ---------------------------------------------------------------------------
# Unicode markdown rendering (iMessage flavor)
# ---------------------------------------------------------------------------
#
# iOS Messages.app does not render Markdown syntax (asterisks, underscores,
# backticks) — it just shows them as literal characters. To get *visual*
# bold/italic/etc. in iMessage we substitute the original characters with
# the matching Unicode "Mathematical Alphanumeric Symbols" range, which
# Apple's font stack renders natively.
#
# Mapping (using code-point arithmetic on ASCII letters / digits):
#
#   A-Z (U+0041..U+005A)  ->  Math Sans-Serif Bold  U+1D5D4..U+1D5ED
#   a-z (U+0061..U+007A)  ->  Math Sans-Serif Bold  U+1D5EE..U+1D607
#   0-9 (U+0030..U+0039)  ->  Math Sans-Serif Bold  U+1D7EC..U+1D7F5
#
#   A-Z                    ->  Math Sans-Serif Italic U+1D608..U+1D621
#   a-z                    ->  Math Sans-Serif Italic U+1D622..U+1D63B
#   h                      ->  Math Sans-Serif Italic (note: no digit forms)
#
#   A-Z                    ->  Math Monospace        U+1D670..U+1D689
#   a-z                    ->  Math Monospace        U+1D68A..U+1D6A3
#   0-9                    ->  Math Monospace        U+1D7F6..U+1D7FF
#
# Strikethrough: prepend U+0336 (COMBINING LONG STROKE OVERLAY) to each char.
# Underline:     prepend U+0332 (COMBINING LOW LINE) to each char.

_BOLD_OFFSET_UPPER = 0x1D5D4 - ord("A")
_BOLD_OFFSET_LOWER = 0x1D5EE - ord("a")
_BOLD_OFFSET_DIGIT = 0x1D7EC - ord("0")

_ITAL_OFFSET_UPPER = 0x1D608 - ord("A")
_ITAL_OFFSET_LOWER = 0x1D622 - ord("a")
# No digit forms for math sans-serif italic; digits left as-is.

_MONO_OFFSET_UPPER = 0x1D670 - ord("A")
_MONO_OFFSET_LOWER = 0x1D68A - ord("a")
_MONO_OFFSET_DIGIT = 0x1D7F6 - ord("0")


def _to_math(text: str, upper_off: int, lower_off: int, digit_off: int) -> str:
    """Apply a Mathematical Alphanumeric Symbols offset to ASCII letters/digits.

    Non-ASCII characters pass through unchanged.
    """
    out_chars = []
    for ch in text:
        code = ord(ch)
        if ord("A") <= code <= ord("Z"):
            out_chars.append(chr(code + upper_off))
        elif ord("a") <= code <= ord("z"):
            out_chars.append(chr(code + lower_off))
        elif ord("0") <= code <= ord("9"):
            out_chars.append(chr(code + digit_off))
        else:
            out_chars.append(ch)
    return "".join(out_chars)


def _to_bold(text: str) -> str:
    return _to_math(text, _BOLD_OFFSET_UPPER, _BOLD_OFFSET_LOWER, _BOLD_OFFSET_DIGIT)


def _to_italic(text: str) -> str:
    return _to_math(text, _ITAL_OFFSET_UPPER, _ITAL_OFFSET_LOWER, 0)


def _to_mono(text: str) -> str:
    return _to_math(text, _MONO_OFFSET_UPPER, _MONO_OFFSET_LOWER, _MONO_OFFSET_DIGIT)


def _to_strikethrough(text: str) -> str:
    # COMBINING LONG STROKE OVERLAY after each char
    return "".join(f"{ch}\u0336" for ch in text)


def _to_underline(text: str) -> str:
    # COMBINING LOW LINE after each char
    return "".join(f"{ch}\u0332" for ch in text)


# Match inline markdown tokens. Order matters — bold before italic because
# **...** would otherwise be eaten by the italic pass. The italic-underscore
# pattern uses a negative lookbehind to require the opening `_` not be
# preceded by a word char (so `snake_case` is not misread as italic), and
# a negative lookahead to require the closing `_` not be followed by a word
# char. Negative lookaheads on the * / _ / ~ openers prevent greedy matches
_INLINE_TOKEN_RE = re.compile(
    r"\*\*(?!\*)[^*\n_][\s\S]*?[^*\n]\*\*"  # **bold** (inner can't start with _)
    r"|\*(?!\*)[^*\n][\s\S]*?[^*\n]\*(?!\*)"  # *italic* (not **)
    r"|__(?!_)[^_\n*][\s\S]*?[^_\n]__"  # __bold__ (inner can't start with *)
    r"|(?<!\w)_(?!_)[^_\n][\s\S]*?[^_\n]_(?!\w)"  # _italic_ (word-boundary)
    r"|~~[^~\n][\s\S]*?[^~\n]~~"  # ~~strike~~
    r"|`[^`\n]+`"  # `code`
    r"|```[^`\n]+```"  # ```code block```
    r"|\[[^\]\n]+\]\([^)\n]+\)"  # [text](url)
)


def _render_inline(text: str) -> str:
    """Render one inline markdown span. Returns the transformed string.

    Handles nesting: ``**__bold-italic__**`` and ``__**bold-italic**__`` both
    apply both transforms (outer bold + inner italic). For ``**__mixed__**``,
    the outer ``**`` is bold and the inner ``__...__`` is a nested italic.
    If a span starts with the *opposite* marker of the outer, the outer
    match is rejected and the inner span is re-rendered standalone.
    """
    # **bold**
    if text.startswith("**") and text.endswith("**") and len(text) >= 4:
        return _to_bold(text[2:-2])
    # __bold__
    if text.startswith("__") and text.endswith("__") and len(text) >= 4:
        return _to_bold(text[2:-2])
    # ~~strikethrough~~
    if text.startswith("~~") and text.endswith("~~") and len(text) >= 4:
        return _to_strikethrough(text[2:-2])
    # `code` or ```code```
    if text.startswith("`") and text.endswith("`") and len(text) >= 2:
        if text.startswith("```") and text.endswith("```") and len(text) >= 6:
            return _to_mono(text[3:-3])
        return _to_mono(text[1:-1])
    # [text](url)
    if text.startswith("[") and "](" in text and text.endswith(")"):
        m = re.match(r"\[([^\]]+)\]\(([^)]+)\)", text)
        if m:
            label, url = m.group(1), m.group(2)
            return f"{label} ({url})"
        return text
    # *italic* (not **)
    if (
        text.startswith("*")
        and text.endswith("*")
        and not text.startswith("**")
        and len(text) >= 2
    ):
        return _to_italic(text[1:-1])
    # _italic_ (not __)
    if (
        text.startswith("_")
        and text.endswith("_")
        and not text.startswith("__")
        and len(text) >= 2
    ):
        return _to_italic(text[1:-1])
    return text


def render_imessage(text: str) -> str:
    """Convert a Telegram-style markdown string to iMessage Unicode.

    Supported tokens (others pass through unchanged):
      **bold**         __bold__           -> 𝗯𝗼𝗹𝗱 (math sans-serif bold)
      *italic*         _italic_           -> 𝘪𝘵𝘢𝘭𝘪𝘤 (math sans-serif italic)
      ~~strike~~                          -> s̶t̶r̶i̶k̶e̶
      `code`           ```code```         -> 𝚌𝚘𝚍𝚎 (math monospace)
      [text](url)                         -> text (url)

    Code blocks (triple-backtick) are protected from nested markdown
    transformation. Headings, lists, and blockquotes pass through unchanged
    (iMessage has no equivalent rendering for them).
    """
    if not text:
        return text

    # 1. Protect triple-backtick code blocks by replacing them with sentinels.
    code_blocks: list[str] = []

    def _stash_code_block(match: re.Match) -> str:
        idx = len(code_blocks)
        code_blocks.append(match.group(0))
        return f"\x00CODEBLOCK{idx}\x00"

    text = re.sub(r"```[^`\n]+```", _stash_code_block, text)

    # 2. Walk inline tokens. Two passes so that nested markers (e.g.
    # ``**__bold-italic__**``) can be peeled outward: the first pass renders
    # the inner span, then the outer marker is re-matched on the now-monospace
    # result. We do this at most twice to bound work and avoid pathological
    # recursion on weird input.
    def _replace_inline(match: re.Match) -> str:
        return _render_inline(match.group(0))

    for _ in range(2):
        text = _INLINE_TOKEN_RE.sub(_replace_inline, text)

    # 3. Restore code blocks, but render their contents as monospace.
    for idx, original in enumerate(code_blocks):
        inner = original[3:-3]  # strip ```
        rendered = _to_mono(inner)
        text = text.replace(f"\x00CODEBLOCK{idx}\x00", rendered)

    return text


# ---------------------------------------------------------------------------
# Helpers (unchanged from v1)
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
        # Watch flags (default: full feed)
        self.enable_reactions: bool = extra.get("enable_reactions", True)
        self.enable_attachments: bool = extra.get("enable_attachments", True)
        # Outbound text rendering
        self.unicode_format: bool = extra.get("unicode_format", True)
        # Default reaction for ack (used by base-class helpers, optional)
        self.default_reaction: str = extra.get("default_reaction", "like")
        # Group chat allowlist: comma-separated chat_ids (numeric rowids) that
        # the agent is allowed to participate in. DMs are always allowed
        # (gated by IMSG_ALLOW_ALL_USERS in authz_mixin). If this is set,
        # inbound messages from any other group chat are dropped before the
        # agent loop. Empty/unset = no group chats allowed.
        self.allowed_group_ids: Set[str] = {
            x.strip()
            for x in extra.get("allowed_group_ids", "").split(",")
            if x.strip()
        }

        self._proc: Optional[asyncio.subprocess.Process] = None
        self._reader_task: Optional[asyncio.Task] = None
        self._running = False

        # Track rowids we've already emitted to avoid duplicates on reconnect
        self._seen_rowids: Set[str] = set()
        self._max_seen_rowids = 1000

        logger.info(
            "ImsgAdapter: cli=%s db=%s reactions=%s attachments=%s unicode=%s",
            self.cli_path,
            _redact(self.db_path),
            self.enable_reactions,
            self.enable_attachments,
            self.unicode_format,
        )
        if self.allowed_group_ids:
            logger.info(
                "ImsgAdapter: group allowlist active (%d chat(s))",
                len(self.allowed_group_ids),
            )

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def connect(self) -> bool:
        """Start ``imsg watch`` subprocess and stream handler."""
        if not self._acquire_platform_lock("imsg", self.platform.value, "imsg adapter"):
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
        """Run ``imsg watch --json`` and emit MessageEvents for each line.

        Supports two event kinds:
        - Regular messages (text-only, attachment-only, or both)
        - Tapback reactions (when --reactions is passed and the event has
          is_reaction: true). Reactions are forwarded to
          ``handle_reaction`` if the base class defines it; otherwise logged.
        """
        backoff = 2.0
        max_backoff = 60.0

        while self._running:
            try:
                cmd = [
                    self.cli_path,
                    "watch",
                    "--json",
                    "--debounce",
                    "250ms",
                ]
                if self.chat_id:
                    cmd.extend(["--chat-id", self.chat_id])
                if self.enable_reactions:
                    cmd.append("--reactions")
                if self.enable_attachments:
                    cmd.append("--attachments")

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
        """Parse one NDJSON line from imsg watch and emit a MessageEvent
        (for messages) or forward a reaction (for tapback events).

        Live JSON shape (verified via ``imsg watch --reactions --json``):

        Message:
          id, chat_id, chat_identifier, chat_guid, chat_name, is_group,
          participants, guid, sender, sender_name, is_from_me, text,
          created_at, attachments[], destination_caller_id, reactions[],
          reply_to_guid, thread_originator_guid

        Reaction extension (when --reactions):
          is_reaction (bool), reaction_type (love|like|dislike|laugh|...),
          reaction_emoji (string, custom emoji if present),
          is_reaction_add (bool), reacted_to_guid (string)
        """
        try:
            msg = json.loads(line)
        except json.JSONDecodeError:
            return

        # --- Reaction event path ---
        if msg.get("is_reaction"):
            await self._handle_reaction_event(msg)
            return

        # --- Regular message path ---

        # Skip our own outgoing messages — only handle inbound (or replies in DMs)
        if msg.get("is_from_me") or msg.get("isFromMe"):
            return

        # Deduplicate by id
        message_rowid = str(msg.get("id") or "")
        if message_rowid and message_rowid in self._seen_rowids:
            return
        if message_rowid:
            self._seen_rowids.add(message_rowid)
            if len(self._seen_rowids) > self._max_seen_rowids:
                # Cap: drop the OLDEST entries to bound memory. Python's
                # set has no ordering, so we materialize to a list, drop
                # the prefix, and replace. This is an O(N) shrink on a
                # ~1000-entry cache, which is cheap.
                excess = len(self._seen_rowids) - self._max_seen_rowids
                keep = list(self._seen_rowids)[excess:]
                self._seen_rowids = set(keep)

        # Extract sender (it's a flat string, not a dict)
        sender_handle = msg.get("sender") or ""
        sender_number = _normalize_phone(sender_handle)
        sender_name = msg.get("sender_name") or msg.get("senderName") or ""

        # Chat ID: prefer the integer rowid (stable, fast for outbound
        # routing via `imsg rpc send --chat_id <int>`). Fall back to
        # `chat_identifier` ("+1...") when no rowid is present (e.g.
        # brand-new group chats that haven't been picked up by the watch
        # yet). Fall back to the sender phone for one-shot DMs.
        rowid = msg.get("chat_id")
        chat_identifier = msg.get("chat_identifier") or ""
        if rowid is not None and rowid != "":
            chat_id = str(rowid)
            if chat_identifier:
                try:
                    CHAT_ROWID_BY_IDENTIFIER[chat_identifier] = int(rowid)
                except (TypeError, ValueError):
                    pass
        elif chat_identifier:
            chat_id = chat_identifier
        else:
            chat_id = sender_number or ""
            # Cache sender -> rowid for the rare case where watch emits a DM
            # with no chat_identifier. imsg normally populates chat_id, but
            # guard against schema drift.
            if sender_number and rowid:
                try:
                    CHAT_ROWID_BY_IDENTIFIER[sender_number] = int(rowid)
                except (TypeError, ValueError):
                    pass

        # Chat type
        is_group = bool(msg.get("is_group", False))
        chat_type = "group" if is_group else "dm"

        # Group chat allowlist gate. DMs always pass (authz_mixin handles
        # the human-vs-allowlist check). For groups, the chat's rowid (or
        # chat_identifier) must be in the allowlist. SAFE DEFAULT: when
        # the allowlist is empty/unset, ALL group messages are dropped.
        # Groups are opt-in — the user has to explicitly add a chat_id to
        # the allowlist before the agent will respond in a group. This
        # prevents the agent from accidentally broadcasting to every
        # group thread that sends it a message.
        if is_group:
            rowid_str = str(rowid) if rowid not in (None, "") else ""
            ident = chat_identifier or ""
            if not self.allowed_group_ids or (
                rowid_str not in self.allowed_group_ids
                and ident not in self.allowed_group_ids
            ):
                logger.debug(
                    "ImsgAdapter: dropping group message from chat_id=%s (not in allowlist)",
                    rowid_str or ident or "?",
                )
                return

        # Message body
        text = msg.get("text") or ""

        # Attachments
        attachments = msg.get("attachments") or []
        has_attachments = len(attachments) > 0

        # If there's no text AND no attachment metadata, drop the event.
        # (Pure empty messages are noise.)
        if not text and not has_attachments:
            return

        # Reply context
        reply_to_guid = msg.get("reply_to_guid") or msg.get("replyToGUID") or None
        reply_to_text = msg.get("replyToText") or None

        # Message type
        msg_type = (
            MessageType.PHOTO if has_attachments and not text else MessageType.TEXT
        )

        # Build MessageEvent
        event = MessageEvent(
            text=text,
            message_type=msg_type,
            message_id=message_rowid or None,
            reply_to_message_id=reply_to_guid,
            reply_to_text=reply_to_text,
            source=self._make_source(
                chat_id=chat_id,
                chat_type=chat_type,
                chat_name=msg.get("chat_name") or sender_name or None,
                sender_number=sender_number,
                sender_name=sender_name,
            ),
            raw_message=msg,
            timestamp=self._parse_timestamp(msg.get("created_at")),
        )

        await self.handle_message(event)

    async def _handle_reaction_event(self, msg: Dict[str, Any]) -> None:
        """Forward a tapback reaction event from imsg watch to the base class.

        We do NOT emit this as a MessageEvent (it has no text body that the
        agent can reply to). The base class may define ``handle_reaction``
        for cross-platform reaction routing; if not, log and drop.

        Fields read:
          - chat_id (int rowid, used for outbound routing on imsg)
          - reacted_to_guid (target message GUID)
          - reaction_type (love | like | dislike | laugh | emphasis | question)
          - reaction_emoji (string, when custom emoji)
          - is_reaction_add (true for add, false for remove)
          - sender (handle)
        """
        chat_id = str(msg.get("chat_id") or msg.get("chat_identifier") or "")
        target_guid = msg.get("reacted_to_guid") or ""
        reaction_type = msg.get("reaction_type") or ""
        reaction_emoji = msg.get("reaction_emoji") or ""
        is_add = bool(msg.get("is_reaction_add", True))
        sender = msg.get("sender") or ""

        label = reaction_emoji or reaction_type or "?"
        verb = "reacted" if is_add else "un-reacted"

        logger.info(
            "ImsgAdapter: reaction event chat=%s target=%s sender=%s %s %s",
            chat_id,
            target_guid[:8] if target_guid else "?",
            _redact(sender) or "?",
            verb,
            label,
        )

        # If the base class wants reactions, give it the raw event.
        # We do not block on this — if it's not implemented, that's fine.
        handler = getattr(self, "handle_reaction", None)
        if callable(handler):
            try:
                maybe_coro = handler(
                    chat_id=chat_id,
                    target_message_guid=target_guid,
                    reaction_type=reaction_type,
                    reaction_emoji=reaction_emoji,
                    is_add=is_add,
                    sender=sender,
                    raw=msg,
                )
                if asyncio.iscoroutine(maybe_coro):
                    await maybe_coro
            except Exception as e:  # pragma: no cover - defensive
                logger.debug("ImsgAdapter: handle_reaction raised: %s", e)

    def _make_source(
        self,
        *,
        chat_id: str,
        chat_type: str,
        chat_name: Optional[str],
        sender_number: str,
        sender_name: str,
    ):
        """Build a SessionSource for this inbound message."""
        from gateway.session import SessionSource

        return SessionSource(
            platform=self.platform,
            chat_id=chat_id,
            chat_name=chat_name or None,
            chat_type=chat_type,
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
    # Outbound: text (imsg rpc), file (imsg send --file),
    # reaction (imsg react), typing (imsg typing)
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

        # Apply iMessage-flavored Unicode rendering unless disabled
        if self.unicode_format:
            content = render_imessage(content)

        if len(content) > MAX_MESSAGE_LENGTH:
            content = content[: MAX_MESSAGE_LENGTH - 3] + "..."

        try:
            # If chat_id is purely numeric, it's an int rowid -> use --chat-id
            params: Dict[str, Any] = {
                "text": content,
                "service": "imessage",
            }
            if chat_id.isdigit():
                # Integer rowid is the stable, fast path. imsg rpc requires
                # the snake_case `chat_id` key, NOT `chatId` (verified
                # against the live CLI: chatId returns -32602 "Invalid
                # params: to is required for direct sends").
                params["chat_id"] = int(chat_id)
            else:
                params["to"] = chat_id

            result = await self._rpc_call("send", params)
            message_id = str(result.get("id") or result.get("messageId") or "")
            return SendResult(success=True, message_id=message_id)
        except Exception as e:
            logger.error("ImsgAdapter send error: %s", e)
            return SendResult(success=False, error=str(e))

    async def send_file(
        self,
        chat_id: str,
        file_path: str,
        caption: Optional[str] = None,
    ) -> SendResult:
        """Send a file attachment via ``imsg send --file``.

        Falls back to ``imsg rpc send`` for the text body, then sends the
        file via a separate ``imsg send --file`` subprocess call.

        Why a subprocess and not the rpc? Because ``imsg rpc`` only
        supports the ``send`` method, and that method does not currently
        accept a file path — ``attachments.send`` returns "Method not
        found" in the rpc interface. The CLI's ``imsg send --file`` is
        the documented way.

        Validates the path is readable and non-empty before invoking imsg.
        """
        if not file_path:
            return SendResult(success=False, error="empty file path")
        path = Path(os.path.expanduser(file_path))
        if not path.is_file():
            return SendResult(success=False, error=f"file not found: {file_path}")

        # If there's a caption, send it as a separate text message first.
        # iMessage does not have a single "send file + caption" API via
        # imsg; the alternative is to use AppleScript directly. We send
        # the caption as plain text and then the file as a follow-up
        # attachment in the same chat, so both land in the same thread.
        if caption:
            caption_rendered = (
                render_imessage(caption) if self.unicode_format else caption
            )
            caption_result = await self.send(chat_id=chat_id, content=caption_rendered)
            if not caption_result.success:
                return SendResult(
                    success=False,
                    error=f"caption send failed: {caption_result.error}",
                )

        cmd = [
            self.cli_path,
            "send",
            "--file",
            str(path),
            "--json",
        ]
        # Routing: int rowid -> --chat-id, otherwise --to (handle/identifier)
        if chat_id.isdigit():
            cmd.extend(["--chat-id", chat_id])
        else:
            cmd.extend(["--to", chat_id])

        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout_data, stderr_data = await proc.communicate()
            if proc.returncode != 0:
                stderr = stderr_data.decode("utf-8", errors="replace").strip()
                return SendResult(
                    success=False,
                    error=f"imsg send --file exited {proc.returncode}: {stderr}",
                )
            # imsg send --json returns {"status":"sent"} or similar
            try:
                payload = json.loads(
                    stdout_data.decode("utf-8", errors="replace").strip()
                )
                message_id = str(
                    payload.get("id")
                    or payload.get("messageId")
                    or payload.get("status", "")
                )
            except json.JSONDecodeError:
                message_id = ""
            return SendResult(success=True, message_id=message_id)
        except Exception as e:
            logger.error("ImsgAdapter send_file error: %s", e)
            return SendResult(success=False, error=str(e))

    async def send_image_file(
        self,
        chat_id: str,
        image_path: str,
        caption: Optional[str] = None,
        reply_to: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> SendResult:
        """Send an image as an iMessage attachment. Delegates to send_file."""
        return await self.send_file(
            chat_id=chat_id, file_path=image_path, caption=caption
        )

    async def _detect_sip_status(self) -> Optional[bool]:
        """On macOS 26+, check whether SIP is enabled via csrutil.

        Returns True if SIP is enabled, False if disabled, None if the
        host is not macOS 26+ or the check could not be performed.
        The result is cached on the instance for the lifetime of the
        process to avoid the overhead of repeated csrutil probes.
        """
        if getattr(self, "_sip_status", None) is not None:
            return self._sip_status
        try:
            import platform

            macos_major = (
                int(platform.mac_ver()[0].split(".")[0]) if platform.mac_ver()[0] else 0
            )
        except Exception:
            macos_major = 0
        if macos_major < 26:
            self._sip_status = None  # not applicable
            return None
        try:
            sip_proc = await asyncio.create_subprocess_exec(
                "/usr/bin/csrutil",
                "status",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            sip_out, _ = await asyncio.wait_for(sip_proc.communicate(), timeout=2.0)
            sip_enabled = b"enabled" in sip_out.lower()
        except Exception:
            sip_enabled = True  # assume enabled if we can't tell
        self._sip_status = sip_enabled
        return sip_enabled

    async def send_typing(self, chat_id: str, metadata=None) -> None:
        """Send a one-shot typing indicator via ``imsg typing``.

        The iMessage typing bubble on the recipient's side lasts ~5–10s
        before the Messages app auto-clears it, so a single fire-and-forget
        call is enough. Failures are non-fatal — typing indicators are
        decorative, and we don't want to break the message loop if imsg
        errors.

        Compatibility notes
        --------------------
        - macOS 26 (Tahoe) + SIP enabled: ``imsg typing`` returns an
          error because ``imagent`` (the Messages daemon) rejects
          third-party clients without Apple-private entitlements. There
          is no fix without disabling SIP, which we do not recommend
          because it weakens macOS security. We detect this once and
          skip subsequent attempts for the lifetime of the process.
        - imsg < 0.11.0: ``imsg typing`` is also broken (couldn't find
          chats even on older macOS). The install recipe pins
          ``imsg >= 0.11.0``.
        - imsg >= 0.11.0: ``imsg typing --to <phone> --duration 8s``
          works on macOS <= 15; the process holds for the duration
          and the bubble is visible until the process exits.
        """
        if not chat_id:
            return
        if getattr(self, "_typing_disabled", False):
            return
        sip_enabled = await self._detect_sip_status()
        if sip_enabled is True:
            self._typing_disabled = True
            logger.warning(
                "ImsgAdapter: typing indicators require SIP disabled on macOS 26+. "
                "Run `csrutil disable` in Recovery Mode to enable, "
                "or accept that typing bubbles won't appear on this thread."
            )
            return
        try:
            # On imsg >= 0.11.0, the working path depends on macOS version
            # and whether SIP is enabled. We try in order:
            #   1. --to <phone_or_email>   (works on macOS <= 15 always,
            #                              on macOS 26 only if SIP disabled)
            #   2. --chat-id <rowid>       (works on macOS <= 15)
            #   3. --chat-identifier       (last resort; not always reliable)
            #
            # We resolve the rowid -> phone/identifier from the local
            # messages DB if we don't already have it. This is a
            # read-only query against the user's own chat.db and is
            # safe to run repeatedly (microseconds).
            lookup_value = chat_id
            if chat_id.isdigit():
                ident = await self._lookup_chat_identifier(chat_id)
                if ident:
                    lookup_value = ident
            cmd = [self.cli_path, "typing"]
            cmd.extend(["--to", lookup_value])
            cmd.extend(["--duration", "8s"])
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            try:
                out, err = await asyncio.wait_for(proc.communicate(), timeout=3.0)
                if proc.returncode != 0:
                    err_text = (err or out).decode(errors="replace").strip()
                    # If --to failed for a numeric rowid, retry with --chat-id
                    # (the legacy macOS <=15 path that accepts rowid directly).
                    if chat_id.isdigit():
                        logger.debug(
                            "ImsgAdapter: --to path failed, retrying with --chat-id %s",
                            chat_id,
                        )
                        await self._typing_subprocess_via_chat_id(chat_id)
                        return
                    logger.warning(
                        "ImsgAdapter: imsg typing returned %d: %s",
                        proc.returncode,
                        err_text[:200],
                    )
            except asyncio.TimeoutError:
                # Process held — typing bubble is open. Don't kill it;
                # let it run for the duration.
                logger.debug("ImsgAdapter: typing bubble open for chat_id=%s", chat_id)
        except Exception as e:
            logger.warning("ImsgAdapter: send_typing failed (non-fatal): %s", e)

    async def _typing_subprocess_via_chat_id(self, chat_id: str) -> None:
        """Legacy macOS <=15 path: ``imsg typing --chat-id <rowid>``."""
        try:
            cmd = [self.cli_path, "typing", "--chat-id", chat_id, "--duration", "8s"]
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            try:
                out, err = await asyncio.wait_for(proc.communicate(), timeout=3.0)
                if proc.returncode != 0:
                    err_text = (err or out).decode(errors="replace").strip()
                    logger.warning(
                        "ImsgAdapter: imsg typing (--chat-id) returned %d: %s",
                        proc.returncode,
                        err_text[:200],
                    )
            except asyncio.TimeoutError:
                logger.debug(
                    "ImsgAdapter: typing bubble open via --chat-id for chat_id=%s",
                    chat_id,
                )
        except Exception as e:
            logger.warning("ImsgAdapter: send_typing fallback failed: %s", e)

    async def _lookup_chat_identifier(self, rowid: str) -> Optional[str]:
        """Resolve a chat rowid to its ``iMessage;-;+1...`` identifier.

        Returns the empty string if the rowid is unknown. The lookup is
        a single indexed SQL query against the user's chat.db and runs
        in microseconds, so it's safe to call on every typing indicator.
        """
        try:
            import sqlite3

            conn = sqlite3.connect(self.db_path, timeout=2.0)
            try:
                cur = conn.execute(
                    "SELECT chat_identifier FROM chat WHERE ROWID = ?",
                    (int(rowid),),
                )
                row = cur.fetchone()
                return (row[0] or "") if row else ""
            finally:
                conn.close()
        except Exception as e:
            logger.debug(
                "ImsgAdapter: _lookup_chat_identifier(%s) failed: %s", rowid, e
            )
            return ""

    async def stop_typing(self, chat_id: str) -> None:
        """Stop a typing indicator. iMessage's typing bubble auto-clears,
        but if Hermes started one we try to stop it explicitly."""
        if not chat_id:
            return
        try:
            cmd = [self.cli_path, "typing", "--stop", "true"]
            if chat_id.isdigit():
                cmd.extend(["--chat-id", chat_id])
            else:
                cmd.extend(["--to", chat_id])
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            try:
                await asyncio.wait_for(proc.communicate(), timeout=2.0)
            except asyncio.TimeoutError:
                try:
                    proc.kill()
                except Exception:
                    pass
        except Exception as e:
            logger.debug("ImsgAdapter: stop_typing failed (non-fatal): %s", e)

    async def react(self, chat_id: str, message_id: str, reaction: str) -> SendResult:
        """Send a tapback reaction to the most recent message in a chat.

        NOTE: ``imsg react`` only reacts to the **most recent** incoming
        message in the conversation. The ``message_id`` argument is
        accepted for API compatibility with the base class but is not
        honored — imsg looks up the target itself via the chat.

        Requires Accessibility permission for the running Hermes process
        (osascript / System Events). Returns a clear error if the
        permission is missing.
        """
        if not chat_id:
            return SendResult(success=False, error="empty chat_id")
        if not reaction:
            return SendResult(success=False, error="empty reaction")
        # Accept either a name from VALID_REACTIONS, or any custom emoji.
        # Custom emoji just pass through to ``imsg react --reaction``.
        cmd = [
            self.cli_path,
            "react",
            "--reaction",
            reaction,
            "--json",
        ]
        if chat_id.isdigit():
            cmd.extend(["--chat-id", chat_id])
        else:
            return SendResult(
                success=False,
                error=(
                    "react requires a numeric chat_id (imsg cli limitation); "
                    f"got {chat_id!r}"
                ),
            )

        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout_data, stderr_data = await proc.communicate()
            if proc.returncode != 0:
                stderr = stderr_data.decode("utf-8", errors="replace").strip()
                # Map common osascript-permission errors to a clearer message
                if "osascript is not allowed" in stderr:
                    return SendResult(
                        success=False,
                        error=(
                            "imsg react needs Accessibility permission for "
                            "the Hermes process. Open System Settings -> "
                            "Privacy & Security -> Accessibility and add "
                            "the running Python binary."
                        ),
                    )
                return SendResult(
                    success=False,
                    error=f"imsg react exited {proc.returncode}: {stderr}",
                )
            try:
                payload = json.loads(
                    stdout_data.decode("utf-8", errors="replace").strip()
                )
                return SendResult(
                    success=True,
                    message_id=str(message_id or ""),
                    raw_response=payload,
                )
            except json.JSONDecodeError:
                return SendResult(success=True, message_id=str(message_id or ""))
        except Exception as e:
            logger.error("ImsgAdapter react error: %s", e)
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
            raise RuntimeError(f"invalid JSON-RPC response: {e}\n{stdout_data!r}")

        if response.get("error"):
            raise RuntimeError(f"JSON-RPC error: {response['error']}")

        return response.get("result") or {}

    # ------------------------------------------------------------------
    # Chat info
    # ------------------------------------------------------------------

    async def get_chat_info(self, chat_id: str) -> Dict[str, Any]:
        """Look up a chat by phone number / address via imsg chats."""
        try:
            proc = await asyncio.create_subprocess_exec(
                self.cli_path,
                "chats",
                "--json",
                "--limit",
                "100",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                limit=65536,
            )
            stdout_data, _ = await proc.communicate()
            if proc.returncode != 0:
                return {"name": chat_id, "type": "dm"}

            chats = json.loads(stdout_data.decode("utf-8", errors="replace"))
            for chat in chats:
                # Match by rowid (numeric) OR by chat_identifier
                if (
                    str(chat.get("id") or "") == chat_id
                    or str(chat.get("chat_id") or "") == chat_id
                    or str(chat.get("identifier") or "") == chat_id
                ):
                    return {
                        "name": chat.get("contact_name")
                        or chat.get("display_name")
                        or chat_id,
                        "type": "group" if chat.get("is_group") else "dm",
                    }
            return {"name": chat_id, "type": "dm"}
        except Exception:
            return {"name": chat_id, "type": "dm"}
