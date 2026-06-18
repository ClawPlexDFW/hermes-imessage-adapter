"""End-to-end test suite for the ImsgAdapter.

Exercises the real adapter code from a fresh Hermes install against a mocked
``imsg`` CLI. Validates:

- Module imports without error
- Helpers (_redact, _normalize_phone, _discover_imsg_path) behave
- iMessage-flavored Unicode rendering of outbound markdown
- _handle_watch_line parses the live imsg JSON shape correctly
- Tapback reaction events are forwarded (not emitted as text messages)
- Group chat routing: is_group flag, chat_id int rowid routing
- Self-loop guard: is_from_me: true messages are dropped
- Deduplication: same id is dropped on second delivery
- Empty / attachment-only messages are dropped
- RPC send: builds correct JSON-RPC 2.0 payload with service="imessage"
- send_file: invokes imsg send --file with the right CLI flags
- react: invokes imsg react subprocess (separate from rpc)
- send_typing: invokes imsg typing subprocess
- Patch: applies cleanly and produces byte-identical live file
- Patch: --forward --batch refuses idempotent re-application

Run with::

    uv run --with pytest --with aiohttp python -m pytest tests/

The tests use ``HERMES_HOME=/tmp/hermes-test-home`` so they don't touch the
real gateway config. SKIP_LIVE_TESTS=1 skips the live integration tests
that require the real Hermes install at /Users/soup/.hermes/hermes-agent.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

# ---------------------------------------------------------------------------
# Path setup: see tests/conftest.py for the full story.
#
# This conftest-managed environment uses stub gateway.* modules under
# tests/_stubs/ so the test suite runs in CI without a Hermes install.
# We re-import the symbols the tests use so the test bodies read cleanly
# (e.g. `PlatformConfig(...)` rather than `conftest.PlatformConfig(...)`).
# ---------------------------------------------------------------------------

REPO_ROOT = Path(__file__).resolve().parent.parent
ADAPTER_PATH = REPO_ROOT / "platforms" / "imsg.py"
PATCH_PATH = REPO_ROOT / "patches" / "authz.patch"
# Checked-in fixtures for install-dry-run tests so they run in CI without
# a live Hermes install. PRE-PATCH = vanilla Hermes state the patch
# applies to; POST-PATCH = the expected state after a clean apply.
PRE_PATCH_AUTHZ_PATH = REPO_ROOT / "tests" / "fixtures" / "authz_mixin_prepatch.py"
POST_PATCH_AUTHZ_PATH = REPO_ROOT / "tests" / "fixtures" / "authz_mixin_postpatch.py"
# Alias kept for backward-compat with test bodies that haven't been
# renamed; new code should reference PRE_PATCH_AUTHZ_PATH / POST_PATCH_AUTHZ_PATH
LIVE_AUTHZ_PATH = POST_PATCH_AUTHZ_PATH

from gateway.config import Platform, PlatformConfig  # noqa: E402
from gateway.platforms import imsg  # noqa: E402  # the repo's adapter, loaded by conftest


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def make_real_imsg_line(**overrides) -> str:
    """Build an NDJSON line shaped exactly like `imsg history --json` output."""
    base = {
        "guid": "F71C91E6-4E0A-4A45-8B5B-C1163D1C66D7",
        "id": 778,
        "chat_id": 4,
        "created_at": "2026-06-18T21:39:28.766Z",
        "sender": "+155****0050",
        "is_from_me": False,
        "text": "Hello",
        "chat_identifier": "+155****0050",
        "chat_guid": "iMessage;-;+155****0050",
        "chat_name": "",
        "destination_caller_id": "tyler.delano@icloud.com",
        "is_group": False,
        "participants": ["+155****0050"],
        "attachments": [],
        "reactions": [],
    }
    base.update(overrides)
    return json.dumps(base)


def make_reaction_line(**overrides) -> str:
    """Build an NDJSON line shaped like a `imsg watch --reactions` event."""
    base = {
        "guid": "REACTION-GUID-001",
        "id": 901,
        "chat_id": 4,
        "created_at": "2026-06-18T22:00:00.000Z",
        "sender": "+155****0050",
        "is_from_me": False,
        "is_reaction": True,
        "reaction_type": "love",
        "reaction_emoji": "",
        "is_reaction_add": True,
        "reacted_to_guid": "F71C91E6-4E0A-4A45-8B5B-C1163D1C66D7",
    }
    base.update(overrides)
    return json.dumps(base)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestHelpers(unittest.TestCase):
    def test_redact_phone_and_email(self):
        # Note: the redaction regex is \d{7,15} — phone numbers with 7+
        # consecutive digits get caught.
        self.assertEqual(
            imsg._redact("call 5555550100 or a@b.com"),
            "call [REDACTED] or [REDACTED]",
        )
        # Edge case: a phone-shaped local part (7+ digits) is caught by
        # the phone regex BEFORE the email regex runs.
        result = imsg._redact("ping 5555550100@sms.com")
        self.assertNotIn("5555550100", result)
        # A normal "hello@x.com" gets fully redacted.
        self.assertEqual(imsg._redact("ping hello@x.com"), "ping [REDACTED]")

    def test_redact_empty(self):
        self.assertEqual(imsg._redact(""), "")
        self.assertEqual(imsg._redact("no pii here"), "no pii here")

    def test_normalize_phone_adds_plus(self):
        # _normalize_phone does NOT add a US country code — it just prepends '+'
        # and strips formatting. A 10-digit input becomes +10-digit.
        self.assertEqual(imsg._normalize_phone("5555550100"), "+5555550100")
        # 11-digit input (with leading 1) becomes +11-digit.
        self.assertEqual(imsg._normalize_phone("15555550100"), "+15555550100")
        # Formatted E.164 with spaces/dashes/parens is cleaned up.
        self.assertEqual(imsg._normalize_phone("+1 555-555-0100"), "+15555550100")
        self.assertEqual(imsg._normalize_phone("(555) 555-0100"), "+5555550100")

    def test_normalize_phone_empty(self):
        self.assertEqual(imsg._normalize_phone(""), "")

    def test_normalize_phone_already_normalized(self):
        self.assertEqual(imsg._normalize_phone("+15555550100"), "+15555550100")

    def test_discover_imsg_path_finds_brew(self):
        result = imsg._discover_imsg_path()
        self.assertTrue(
            result.endswith("imsg"),
            f"expected path to end with 'imsg', got {result!r}",
        )

    def test_check_imsg_requirements(self):
        self.assertTrue(
            imsg.check_imsg_requirements(),
            "imsg CLI not on PATH — run: brew install steipete/tap/imsg",
        )

    def test_adapter_class_metadata(self):
        self.assertEqual(imsg.ImsgAdapter.platform, Platform.IMSG)
        self.assertFalse(imsg.ImsgAdapter.SUPPORTS_MESSAGE_EDITING)
        self.assertEqual(imsg.ImsgAdapter.MAX_MESSAGE_LENGTH, 4000)
        patterns = imsg.ImsgAdapter.DEFAULT_MENTION_PATTERNS
        joined = " ".join(patterns)
        self.assertIn("hermes", joined)
        self.assertIn("hoss", joined)

    def test_valid_reactions_constant(self):
        # The imsg CLI accepts a fixed set of named reactions plus custom
        # emoji. Make sure the documented set matches the CLI.
        self.assertEqual(
            imsg.VALID_REACTIONS,
            {"love", "like", "dislike", "laugh", "emphasis", "question"},
        )


# ---------------------------------------------------------------------------
# Unicode rendering tests
# ---------------------------------------------------------------------------


class TestUnicodeRendering(unittest.TestCase):
    """Verify render_imessage converts Telegram-flavored markdown to
    iMessage-flavored Unicode (Math Sans-Serif, Math Mono, etc.)."""

    def test_bold_double_asterisk(self):
        # **bold** -> math sans-serif bold (𝗯𝗼𝗹𝗱)
        self.assertEqual(imsg.render_imessage("**bold**"), "𝗯𝗼𝗹𝗱")

    def test_bold_double_underscore(self):
        # __bold__ -> same math sans-serif bold
        self.assertEqual(imsg.render_imessage("__bold__"), "𝗯𝗼𝗹𝗱")

    def test_italic_single_asterisk(self):
        # *italic* -> math sans-serif italic (𝘪𝘵𝘢𝘭𝘪𝘤)
        self.assertEqual(imsg.render_imessage("*italic*"), "𝘪𝘵𝘢𝘭𝘪𝘤")

    def test_italic_single_underscore(self):
        # _italic_ -> same math sans-serif italic
        self.assertEqual(imsg.render_imessage("_italic_"), "𝘪𝘵𝘢𝘭𝘪𝘤")

    def test_inline_code(self):
        # `code` -> math monospace (𝚌𝚘𝚍𝚎)
        self.assertEqual(imsg.render_imessage("`code`"), "𝚌𝚘𝚍𝚎")

    def test_strikethrough(self):
        # ~~strike~~ -> COMBINING LONG STROKE OVERLAY (s̶t̶r̶i̶k̶e̶)
        self.assertEqual(imsg.render_imessage("~~strike~~"), "s̶t̶r̶i̶k̶e̶")

    def test_link_renders_as_text_url(self):
        # [text](url) -> "text (url)" (iMessage shows URLs as plain text)
        self.assertEqual(
            imsg.render_imessage("[Click](https://example.com)"),
            "Click (https://example.com)",
        )

    def test_mixed_bold_and_italic(self):
        # **bold** *italic* both rendered
        result = imsg.render_imessage("**bold** and *italic*")
        self.assertIn("𝗯𝗼𝗹𝗱", result)
        self.assertIn("𝘪𝘵𝘢𝘭𝘪𝘤", result)
        # No literal asterisks remain
        self.assertNotIn("**", result)
        self.assertNotIn(" *", result)

    def test_snake_case_not_mangled(self):
        # snake_case identifiers must not be misread as italic
        self.assertEqual(
            imsg.render_imessage("use snake_case and a_b_c_d"),
            "use snake_case and a_b_c_d",
        )

    def test_plain_text_passes_through(self):
        self.assertEqual(
            imsg.render_imessage("hello, plain text here"),
            "hello, plain text here",
        )

    def test_empty_string(self):
        self.assertEqual(imsg.render_imessage(""), "")

    def test_code_block_protected(self):
        # ```code``` should render contents as monospace, not transform inside
        result = imsg.render_imessage("```**not bold**```")
        # The ** is inside code, so the asterisks should be preserved
        # literally (NOT transformed into math monospace which would
        # remove the asterisks).
        self.assertIn("**", result)
        # The letters inside should be in math monospace range (U+1D670..U+1D7FF)
        # The 'n' from "not" should be 𝚗
        self.assertIn("𝚗", result)

    def test_hello_world_real_case(self):
        # The exact phrase from the user's complaint
        result = imsg.render_imessage("Hello **World**")
        # "World" should be math sans-serif bold (𝗪𝗼𝗿𝗹𝗱)
        self.assertEqual(result, "Hello 𝗪𝗼𝗿𝗹𝗱")
        # Verify the characters are in the right range
        for ch in result.split(" ")[1]:
            self.assertGreaterEqual(ord(ch), 0x1D400)
            self.assertLessEqual(ord(ch), 0x1D7FF)

    def test_digits_in_bold(self):
        # **abc123** -> bold including digits (math sans-serif has digit forms)
        result = imsg.render_imessage("**abc123**")
        # All chars should be in math sans-serif bold range
        for ch in result:
            self.assertGreaterEqual(ord(ch), 0x1D400)
            self.assertLessEqual(ord(ch), 0x1D7FF)

    def test_unclosed_marker_passes_through(self):
        # An unclosed ** is not a valid span — leave it literal
        result = imsg.render_imessage("hello **world")
        self.assertEqual(result, "hello **world")

    def test_newlines_preserved(self):
        result = imsg.render_imessage("**line1**\n*line2*")
        self.assertIn("𝗹𝗶𝗻𝗲𝟭", result)
        self.assertIn("\n", result)
        self.assertIn("𝘭𝘪𝘯𝘦2", result)

    def test_emoji_preserved(self):
        # Emoji are multi-codepoint; they should pass through unchanged
        result = imsg.render_imessage("hello **world** 🦊")
        self.assertIn("🦊", result)
        self.assertIn("𝘄𝗼𝗿𝗹𝗱", result)


# ---------------------------------------------------------------------------
# Watch line parsing tests (inbound events)
# ---------------------------------------------------------------------------


class TestWatchLineParsing(unittest.IsolatedAsyncioTestCase):
    """Test _handle_watch_line against the real imsg JSON shape."""

    async def asyncSetUp(self):
        cfg = PlatformConfig(enabled=True, extra={})
        self.adapter = imsg.ImsgAdapter(cfg)
        # Replace handle_message with a recorder so we can assert on it
        self.received: list = []
        self.adapter.handle_message = AsyncMock(
            side_effect=lambda evt: self.received.append(evt)
        )
        # Track reaction events too
        self.reaction_events: list = []
        self.adapter.handle_reaction = AsyncMock(
            side_effect=lambda **kw: self.reaction_events.append(kw)
        )

    async def test_inbound_text_message(self):
        line = make_real_imsg_line(text="Hello", id=1001, is_from_me=False)
        await self.adapter._handle_watch_line(line)
        self.assertEqual(len(self.received), 1)
        evt = self.received[0]
        self.assertEqual(evt.text, "Hello")
        self.assertEqual(evt.message_id, "1001")
        # chat_id is the chat rowid (int as str) — the stable outbound
        # routing key. Sender's phone is the user_id, not the chat_id.
        self.assertEqual(evt.source.chat_id, "4")
        self.assertEqual(evt.source.user_id, "+155****0050")
        self.assertEqual(evt.source.chat_type, "dm")

    async def test_inbound_populates_chat_rowid_cache(self):
        """Regression: every inbound message with a rowid+chat_identifier
        pair must populate CHAT_ROWID_BY_IDENTIFIER so that the imsg-tools
        plugin can resolve a non-numeric chat_id to the rowid required by
        ``imsg react --chat-id``. Pre-fix: the cache did not exist, and
        the plugin's fallback branches (--to, --chat-identifier) called
        imsg with invalid flags, causing silent rc=1 failures.
        """
        from gateway.platforms.imsg import CHAT_ROWID_BY_IDENTIFIER  # type: ignore
        # Baseline: clear any prior mapping for the test identifier.
        CHAT_ROWID_BY_IDENTIFIER.pop("iMessage;-;+155****0050", None)

        line_obj = json.loads(make_real_imsg_line(id=2001, text="hi"))
        line_obj["chat_id"] = 7  # rowid
        line_obj["chat_identifier"] = "iMessage;-;+155****0050"
        await self.adapter._handle_watch_line(json.dumps(line_obj))

        # The cache must now map chat_identifier -> rowid.
        self.assertEqual(
            CHAT_ROWID_BY_IDENTIFIER.get("iMessage;-;+155****0050"), 7
        )
        # And the routed event still uses the rowid as chat_id.
        self.assertEqual(self.received[0].source.chat_id, "7")

    async def test_inbound_no_rowid_does_not_pollute_cache(self):
        """If the watch event has no rowid, the cache must not be
        populated with a string key. The plugin relies on the cache
        to map identifiers to integer rowids.
        """
        from gateway.platforms.imsg import CHAT_ROWID_BY_IDENTIFIER  # type: ignore
        CHAT_ROWID_BY_IDENTIFIER.pop("iMessage;+;norowid", None)

        # Group without rowid: the adapter falls back to chat_identifier
        # for routing but the cache should not get a non-int value.
        cfg = PlatformConfig(
            enabled=True,
            extra={"allowed_group_ids": "iMessage;+;norowid"},
        )
        adapter = imsg.ImsgAdapter(cfg)
        adapter.handle_message = AsyncMock(
            side_effect=lambda evt: self.received.append(evt)
        )
        line_obj = json.loads(make_real_imsg_line(id=2002, text="hi"))
        line_obj.pop("chat_id", None)
        line_obj["is_group"] = True
        line_obj["chat_identifier"] = "iMessage;+;norowid"
        await adapter._handle_watch_line(json.dumps(line_obj))

        # The cache must not contain the identifier (it has no rowid).
        self.assertNotIn("iMessage;+;norowid", CHAT_ROWID_BY_IDENTIFIER)

    async def test_inbound_group_message(self):
        """A message with is_group=True must be routed with chat_type='group'."""
        # Groups are gated by allowed_group_ids; add this chat to the allowlist
        cfg = PlatformConfig(enabled=True, extra={"allowed_group_ids": "42"})
        adapter = imsg.ImsgAdapter(cfg)
        adapter.handle_message = AsyncMock(
            side_effect=lambda evt: self.received.append(evt)
        )
        line_obj = json.loads(make_real_imsg_line(id=1100, text="hey team"))
        line_obj["is_group"] = True
        line_obj["chat_id"] = 42
        line_obj["chat_identifier"] = "iMessage;+;abc123group"
        line_obj["chat_name"] = "Family Group"
        line_obj["participants"] = ["+155****0050", "+155****0100"]
        await adapter._handle_watch_line(json.dumps(line_obj))
        self.assertEqual(len(self.received), 1)
        evt = self.received[0]
        self.assertEqual(evt.source.chat_type, "group")
        # chat_id is the int rowid (stable, used for outbound react/send).
        # chat_identifier is opaque and only matters for diagnostic logs.
        self.assertEqual(evt.source.chat_id, "42")
        self.assertEqual(evt.source.chat_name, "Family Group")

    async def test_inbound_group_message_uses_identifier_when_no_rowid(self):
        """If the watch event has no rowid (brand-new group), fall back to
        chat_identifier so the event still routes."""
        cfg = PlatformConfig(
            enabled=True,
            extra={"allowed_group_ids": "iMessage;+;newgroup999"},
        )
        adapter = imsg.ImsgAdapter(cfg)
        adapter.handle_message = AsyncMock(
            side_effect=lambda evt: self.received.append(evt)
        )
        line_obj = json.loads(make_real_imsg_line(id=1101, text="hello group"))
        line_obj["is_group"] = True
        # Remove the rowid to simulate the edge case
        line_obj.pop("chat_id", None)
        line_obj["chat_identifier"] = "iMessage;+;newgroup999"
        await adapter._handle_watch_line(json.dumps(line_obj))
        self.assertEqual(len(self.received), 1)
        self.assertEqual(self.received[0].source.chat_id, "iMessage;+;newgroup999")

    # --- Group allowlist tests ---

    async def test_group_message_dropped_when_not_in_allowlist(self):
        """With allowed_group_ids set, group messages from other chats are
        dropped silently — the agent never sees them."""
        cfg = PlatformConfig(enabled=True, extra={"allowed_group_ids": "7,8,9"})
        adapter = imsg.ImsgAdapter(cfg)
        adapter.handle_message = AsyncMock(
            side_effect=lambda evt: self.received.append(evt)
        )
        line_obj = json.loads(make_real_imsg_line(id=1200, text="hi"))
        line_obj["is_group"] = True
        line_obj["chat_id"] = 42  # not in {7,8,9}
        await adapter._handle_watch_line(json.dumps(line_obj))
        self.assertEqual(self.received, [])

    async def test_group_message_passed_when_in_allowlist(self):
        """A group message whose rowid is in allowed_group_ids is delivered."""
        cfg = PlatformConfig(enabled=True, extra={"allowed_group_ids": "7,8,42"})
        adapter = imsg.ImsgAdapter(cfg)
        adapter.handle_message = AsyncMock(
            side_effect=lambda evt: self.received.append(evt)
        )
        line_obj = json.loads(make_real_imsg_line(id=1201, text="hi"))
        line_obj["is_group"] = True
        line_obj["chat_id"] = 42
        await adapter._handle_watch_line(json.dumps(line_obj))
        self.assertEqual(len(self.received), 1)
        self.assertEqual(self.received[0].source.chat_type, "group")

    async def test_group_allowlist_matches_by_identifier(self):
        """If a group has no rowid, the allowlist falls back to matching the
        chat_identifier string."""
        cfg = PlatformConfig(
            enabled=True, extra={"allowed_group_ids": "iMessage;+;abc123group"}
        )
        adapter = imsg.ImsgAdapter(cfg)
        adapter.handle_message = AsyncMock(
            side_effect=lambda evt: self.received.append(evt)
        )
        line_obj = json.loads(make_real_imsg_line(id=1202, text="hi"))
        line_obj["is_group"] = True
        line_obj.pop("chat_id", None)
        line_obj["chat_identifier"] = "iMessage;+;abc123group"
        await adapter._handle_watch_line(json.dumps(line_obj))
        self.assertEqual(len(self.received), 1)

    async def test_empty_allowlist_means_no_groups_allowed(self):
        """With an empty allowlist, ALL group messages are dropped (DMs still
        pass). This is the default for safety: groups are opt-in."""
        cfg = PlatformConfig(enabled=True, extra={})  # allowed_group_ids unset
        adapter = imsg.ImsgAdapter(cfg)
        adapter.handle_message = AsyncMock(
            side_effect=lambda evt: self.received.append(evt)
        )
        line_obj = json.loads(make_real_imsg_line(id=1203, text="hi"))
        line_obj["is_group"] = True
        line_obj["chat_id"] = 42
        await adapter._handle_watch_line(json.dumps(line_obj))
        self.assertEqual(self.received, [])

    async def test_dm_always_passes_regardless_of_allowlist(self):
        """DMs are never gated by the group allowlist — only group chats are."""
        cfg = PlatformConfig(enabled=True, extra={"allowed_group_ids": "7,8,9"})
        adapter = imsg.ImsgAdapter(cfg)
        adapter.handle_message = AsyncMock(
            side_effect=lambda evt: self.received.append(evt)
        )
        line = make_real_imsg_line(id=1204, text="hi")
        # is_group=False (default) — this is a DM
        await adapter._handle_watch_line(line)
        self.assertEqual(len(self.received), 1)
        self.assertEqual(self.received[0].source.chat_type, "dm")

    async def test_self_message_dropped(self):
        """is_from_me: true must never produce a MessageEvent — the loop bug."""
        line = make_real_imsg_line(
            text="Hey Hoss — clean signal received.",
            id=1002,
            is_from_me=True,
            sender="+155****0050",
        )
        await self.adapter._handle_watch_line(line)
        self.assertEqual(self.received, [], "self-message leaked through filter")

    async def test_camelcase_isFromMe_also_dropped(self):
        """Older imsg versions emit isFromMe (camelCase). Must also be filtered."""
        line_obj = json.loads(make_real_imsg_line(id=1003, text="legacy"))
        line_obj.pop("is_from_me")
        line_obj["isFromMe"] = True
        line = json.dumps(line_obj)
        await self.adapter._handle_watch_line(line)
        self.assertEqual(self.received, [])

    async def test_dedup_by_rowid(self):
        """Same id delivered twice = one event. The rowid cache prevents re-fire."""
        line = make_real_imsg_line(id=1004, text="Hello")
        await self.adapter._handle_watch_line(line)
        await self.adapter._handle_watch_line(line)  # re-delivery
        self.assertEqual(len(self.received), 1)

    async def test_empty_text_dropped(self):
        line = make_real_imsg_line(id=1005, text="")
        await self.adapter._handle_watch_line(line)
        self.assertEqual(self.received, [])

    async def test_attachment_only_dropped_when_no_text(self):
        """imsg can send {text: "", attachments: [...]} — those are not pure text msgs.

        We surface them as a MessageEvent with message_type=PHOTO so the agent
        can decide to fetch the attachment via imsg's attachment retrieval."""
        line = make_real_imsg_line(
            id=1006, text="", attachments=[{"filename": "photo.jpg"}]
        )
        await self.adapter._handle_watch_line(line)
        # We DO emit a MessageEvent for attachment-only messages (PHOTO type)
        self.assertEqual(len(self.received), 1)
        self.assertEqual(self.received[0].message_type.value, "photo")

    async def test_attachment_only_dropped_when_really_empty(self):
        """No text AND no attachments = drop. (Pure noise like typing indicators.)"""
        line = make_real_imsg_line(id=1007, text="", attachments=[])
        await self.adapter._handle_watch_line(line)
        self.assertEqual(self.received, [])

    async def test_malformed_json_dropped_silently(self):
        """Corrupt NDJSON must not crash the watch loop."""
        await self.adapter._handle_watch_line("not json at all")
        await self.adapter._handle_watch_line("")
        await self.adapter._handle_watch_line('{"incomplete":')
        self.assertEqual(self.received, [])

    async def test_reply_context_preserved(self):
        line_obj = json.loads(make_real_imsg_line(id=1007, text="Yes"))
        line_obj["reply_to_guid"] = "F71C91E6-4E0A-4A45-8B5B-C1163D1C66D7"
        line_obj["replyToText"] = "Are you ready?"
        line = json.dumps(line_obj)
        await self.adapter._handle_watch_line(line)
        self.assertEqual(len(self.received), 1)
        evt = self.received[0]
        self.assertEqual(
            evt.reply_to_message_id, "F71C91E6-4E0A-4A45-8B5B-C1163D1C66D7"
        )
        self.assertEqual(evt.reply_to_text, "Are you ready?")

    # --- Reaction event tests ---

    async def test_reaction_event_forwarded_not_emitted(self):
        """A tapback reaction event must NOT be emitted as a text message.
        It should be forwarded to handle_reaction."""
        line = make_reaction_line(
            reaction_type="love",
            reacted_to_guid="F71C91E6-4E0A-4A45-8B5B-C1163D1C66D7",
            is_reaction_add=True,
        )
        await self.adapter._handle_watch_line(line)
        # No text message
        self.assertEqual(self.received, [])
        # Reaction forwarded
        self.assertEqual(len(self.reaction_events), 1)
        evt = self.reaction_events[0]
        self.assertEqual(evt["reaction_type"], "love")
        self.assertEqual(
            evt["target_message_guid"],
            "F71C91E6-4E0A-4A45-8B5B-C1163D1C66D7",
        )
        self.assertTrue(evt["is_add"])
        self.assertEqual(evt["chat_id"], "4")  # int rowid as string

    async def test_reaction_event_with_custom_emoji(self):
        line_obj = json.loads(make_reaction_line(reaction_type=""))
        line_obj["reaction_emoji"] = "🎉"
        await self.adapter._handle_watch_line(json.dumps(line_obj))
        # No text message should be emitted
        self.assertEqual(len(self.received), 0)
        # Reaction should be forwarded with the custom emoji
        self.assertEqual(len(self.reaction_events), 1)
        self.assertEqual(self.reaction_events[0]["reaction_emoji"], "🎉")

    async def test_reaction_remove_event(self):
        line = make_reaction_line(is_reaction_add=False)
        await self.adapter._handle_watch_line(line)
        self.assertEqual(len(self.reaction_events), 1)
        self.assertFalse(self.reaction_events[0]["is_add"])

    async def test_reaction_with_no_base_class_handler(self):
        """If the base class doesn't define handle_reaction, the event is just
        logged — no crash, no leak."""
        # Override to remove the handler
        adapter = imsg.ImsgAdapter(PlatformConfig(enabled=True, extra={}))
        adapter.handle_message = AsyncMock()
        # Don't set handle_reaction — base class doesn't have it
        # Should not raise
        line = make_reaction_line()
        await adapter._handle_watch_line(line)  # no error


# ---------------------------------------------------------------------------
# RPC send tests (outbound text)
# ---------------------------------------------------------------------------


class TestRPCSend(unittest.IsolatedAsyncioTestCase):
    """Test the outbound send() path against a mocked imsg subprocess."""

    async def asyncSetUp(self):
        cfg = PlatformConfig(enabled=True, extra={})
        self.adapter = imsg.ImsgAdapter(cfg)
        self.adapter.cli_path = "/opt/homebrew/bin/imsg"
        self.captured_stdin: list[bytes] = []

    def _make_mock_proc(self, response_obj: dict, returncode: int = 0):
        proc = MagicMock()
        proc.returncode = returncode
        response_bytes = (json.dumps(response_obj) + "\n").encode()

        async def fake_communicate(input=None):
            if input is not None:
                self.captured_stdin.append(input)
            return response_bytes, b""

        proc.communicate = fake_communicate
        return proc

    def _patch_subprocess(self, proc):
        async def fake_exec(*args, **kwargs):
            self.last_cmd = list(args)
            return proc

        return patch("asyncio.create_subprocess_exec", side_effect=fake_exec)

    async def test_send_builds_correct_jsonrpc(self):
        """send() must produce JSON-RPC 2.0 with method=send, service=imessage."""
        response = {
            "jsonrpc": "2.0",
            "id": 1,
            "result": {"id": 999, "messageId": "abc-123"},
        }
        proc = self._make_mock_proc(response)

        with self._patch_subprocess(proc):
            result = await self.adapter.send(
                chat_id="+15555550100", content="hello back"
            )

        # The subprocess must have been called with: imsg rpc --json
        self.assertEqual(self.last_cmd[:3], ["/opt/homebrew/bin/imsg", "rpc", "--json"])

        # The request payload written to stdin must be JSON-RPC 2.0
        self.assertEqual(len(self.captured_stdin), 1)
        sent = self.captured_stdin[0].decode()
        payload = json.loads(sent)
        self.assertEqual(payload["jsonrpc"], "2.0")
        self.assertEqual(payload["method"], "send")
        self.assertEqual(payload["params"]["to"], "+15555550100")
        self.assertEqual(payload["params"]["text"], "hello back")
        # v1 bug regression test: "service" must be lowercase
        self.assertEqual(payload["params"]["service"], "imessage")

        # And the adapter must report success
        self.assertTrue(result.success)
        self.assertEqual(result.message_id, "999")

    async def test_send_routes_to_chat_id_for_numeric_input(self):
        """If chat_id is purely numeric, route via chat_id (int rowid).
        This is the group-chat routing path — group chats have int rowids
        as their chat_id.
        """
        response = {"jsonrpc": "2.0", "id": 1, "result": {"id": 1}}
        proc = self._make_mock_proc(response)

        with self._patch_subprocess(proc):
            result = await self.adapter.send(chat_id="42", content="group msg")

        self.assertTrue(result.success)
        sent = json.loads(self.captured_stdin[0].decode())
        # Numeric chat_id should use chat_id (snake_case), not to.
        # imsg rpc requires snake_case — chatId returns -32602.
        self.assertEqual(sent["params"]["chat_id"], 42)
        self.assertNotIn("to", sent["params"])

    async def test_send_routes_to_address_for_handle_input(self):
        """A non-numeric chat_id (handle, identifier) routes via to=..."""
        response = {"jsonrpc": "2.0", "id": 1, "result": {"id": 1}}
        proc = self._make_mock_proc(response)

        with self._patch_subprocess(proc):
            await self.adapter.send(chat_id="+155****0100", content="dm msg")

        sent = json.loads(self.captured_stdin[0].decode())
        self.assertEqual(sent["params"]["to"], "+155****0100")
        self.assertNotIn("chat_id", sent["params"])

    async def test_send_applies_unicode_formatting_by_default(self):
        """With unicode_format=True (default), **bold** becomes 𝗯𝗼𝗹𝗱 before send."""
        response = {"jsonrpc": "2.0", "id": 1, "result": {"id": 1}}
        proc = self._make_mock_proc(response)

        with self._patch_subprocess(proc):
            await self.adapter.send(chat_id="+15555550100", content="**hello**")

        sent = json.loads(self.captured_stdin[0].decode())
        # Should contain math sans-serif bold "hello" (𝗵𝗲𝗹𝗹𝗼), not literal **hello**
        self.assertNotIn("**", sent["params"]["text"])
        self.assertIn("𝗵𝗲𝗹𝗹𝗼", sent["params"]["text"])

    async def test_send_skips_unicode_formatting_when_disabled(self):
        """With unicode_format=False, **bold** is sent as literal markdown."""
        cfg = PlatformConfig(enabled=True, extra={"unicode_format": False})
        adapter = imsg.ImsgAdapter(cfg)
        adapter.cli_path = "/opt/homebrew/bin/imsg"
        # Reset captured_stdin
        self.captured_stdin = []

        response = {"jsonrpc": "2.0", "id": 1, "result": {"id": 1}}
        proc = self._make_mock_proc(response)

        with self._patch_subprocess(proc):
            await adapter.send(chat_id="+15555550100", content="**hello**")

        sent = json.loads(self.captured_stdin[0].decode())
        self.assertEqual(sent["params"]["text"], "**hello**")

    async def test_send_truncates_overlong_content(self):
        """iMessage soft limit is 4000 chars; longer must be truncated."""
        long_text = "x" * 5000
        response = {"jsonrpc": "2.0", "id": 1, "result": {"id": 1}}
        proc = self._make_mock_proc(response)

        with self._patch_subprocess(proc):
            await self.adapter.send(chat_id="+15555550100", content=long_text)

        sent = json.loads(self.captured_stdin[0].decode())
        self.assertLessEqual(len(sent["params"]["text"]), 4000)
        self.assertTrue(sent["params"]["text"].endswith("..."))

    async def test_send_empty_content_rejected(self):
        result = await self.adapter.send(chat_id="+15555550100", content="")
        self.assertFalse(result.success)
        self.assertEqual(result.error, "empty content")

    async def test_send_propagates_rpc_error(self):
        """If imsg returns a JSON-RPC error, the adapter should surface it."""
        response = {
            "jsonrpc": "2.0",
            "id": 1,
            "error": {
                "code": -32602,
                "message": "Invalid params",
                "data": "invalid service",
            },
        }
        proc = self._make_mock_proc(response)

        with self._patch_subprocess(proc):
            result = await self.adapter.send(chat_id="+15555550100", content="hi")
        self.assertFalse(result.success)
        self.assertIn("invalid service", result.error)

    async def test_send_propagates_subprocess_nonzero_exit(self):
        """If imsg exits non-zero, the adapter should report stderr."""
        proc = self._make_mock_proc({}, returncode=1)
        proc.communicate = AsyncMock(  # type: ignore[method-override]
            return_value=(b"", b"connection refused")
        )

        with self._patch_subprocess(proc):
            result = await self.adapter.send(chat_id="+15555550100", content="hi")
        self.assertFalse(result.success)
        self.assertIn("connection refused", result.error)


# ---------------------------------------------------------------------------
# send_file tests (attachment send)
# ---------------------------------------------------------------------------


class TestSendFile(unittest.IsolatedAsyncioTestCase):
    """Test the send_file path — invokes imsg send --file subprocess."""

    async def asyncSetUp(self):
        cfg = PlatformConfig(enabled=True, extra={})
        self.adapter = imsg.ImsgAdapter(cfg)
        self.adapter.cli_path = "/opt/homebrew/bin/imsg"
        self.last_cmd = None

    def _patch_subprocess(self, returncode: int = 0, stdout: bytes = b""):
        async def fake_exec(*args, **kwargs):
            self.last_cmd = list(args)

            proc = MagicMock()
            proc.returncode = returncode

            async def fake_communicate(input=None):
                return stdout, b""

            proc.communicate = fake_communicate
            return proc

        return patch("asyncio.create_subprocess_exec", side_effect=fake_exec)

    async def test_send_file_with_handle_routes_to(self):
        with tempfile.NamedTemporaryFile(suffix=".jpg", delete=False) as f:
            f.write(b"fake jpeg bytes")
            path = f.name

        try:
            with self._patch_subprocess(stdout=b'{"status":"sent"}\n'):
                result = await self.adapter.send_file(
                    chat_id="+15555550100",
                    file_path=path,
                )
            self.assertTrue(result.success, result.error)
            # Verify CLI invocation
            self.assertIn("send", self.last_cmd)
            self.assertIn("--file", self.last_cmd)
            self.assertIn(path, self.last_cmd)
            self.assertIn("--to", self.last_cmd)
            self.assertIn("+15555550100", self.last_cmd)
            # Should NOT have --chat-id for handle routing
            self.assertNotIn("--chat-id", self.last_cmd)
        finally:
            os.unlink(path)

    async def test_send_file_with_numeric_chat_id_routes_chat_id(self):
        with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as f:
            f.write(b"fake png")
            path = f.name

        try:
            with self._patch_subprocess(stdout=b'{"status":"sent"}\n'):
                result = await self.adapter.send_file(
                    chat_id="42",  # int rowid for a group chat
                    file_path=path,
                )
            self.assertTrue(result.success)
            # Group chat routing: --chat-id 42
            self.assertIn("--chat-id", self.last_cmd)
            self.assertIn("42", self.last_cmd)
            self.assertNotIn("--to", self.last_cmd)
        finally:
            os.unlink(path)

    async def test_send_file_rejects_nonexistent_path(self):
        result = await self.adapter.send_file(
            chat_id="+15555550100", file_path="/nonexistent/path/xyz.jpg"
        )
        self.assertFalse(result.success)
        self.assertIn("not found", result.error)

    async def test_send_file_with_caption_sends_text_first(self):
        """If a caption is provided, it's sent as a text message first,
        then the file. This is how iMessage's "send file + caption" pattern
        works through the imsg CLI."""
        with tempfile.NamedTemporaryFile(suffix=".jpg", delete=False) as f:
            f.write(b"x")
            path = f.name

        # Capture all subprocess calls in order
        all_calls: list[list] = []
        self.last_call = None

        async def fake_exec(*args, **kwargs):
            all_calls.append(list(args))
            self.last_call = list(args)
            proc = MagicMock()
            proc.returncode = 0
            stdout = b'{"ok":true}\n' if "rpc" in args else b'{"status":"sent"}\n'

            async def fake_communicate(input=None):
                return stdout, b""

            proc.communicate = fake_communicate
            return proc

        try:
            with patch("asyncio.create_subprocess_exec", side_effect=fake_exec):
                result = await self.adapter.send_file(
                    chat_id="+155****0100",
                    file_path=path,
                    caption="Check this out",
                )
            self.assertTrue(result.success, result.error)
            # Two subprocess calls: rpc send (caption) + send --file (file)
            self.assertEqual(
                len(all_calls), 2, f"expected 2 calls, got {len(all_calls)}"
            )
            # First call: rpc send with caption text
            self.assertIn("rpc", all_calls[0])
            # Second call: send --file
            self.assertIn("send", all_calls[1])
            self.assertIn("--file", all_calls[1])
            self.assertIn(path, all_calls[1])
        finally:
            os.unlink(path)


# ---------------------------------------------------------------------------
# react tests (tapback send)
# ---------------------------------------------------------------------------


class TestReact(unittest.IsolatedAsyncioTestCase):
    """Test the react() path — invokes imsg react subprocess (not rpc)."""

    async def asyncSetUp(self):
        cfg = PlatformConfig(enabled=True, extra={})
        self.adapter = imsg.ImsgAdapter(cfg)
        self.adapter.cli_path = "/opt/homebrew/bin/imsg"
        self.last_cmd = None

    def _patch_subprocess(self, returncode: int = 0, stderr: bytes = b""):
        async def fake_exec(*args, **kwargs):
            self.last_cmd = list(args)

            proc = MagicMock()
            proc.returncode = returncode

            async def fake_communicate(input=None):
                return b"", stderr

            proc.communicate = fake_communicate
            return proc

        return patch("asyncio.create_subprocess_exec", side_effect=fake_exec)

    async def test_react_invokes_imsg_react_cli(self):
        with self._patch_subprocess():
            result = await self.adapter.react(
                chat_id="42",  # numeric -> use --chat-id
                message_id="800",  # ignored by imsg but accepted for API compat
                reaction="love",
            )
        self.assertTrue(result.success)
        # Must call `react`, not `rpc` — react is a separate CLI subcommand
        self.assertIn("react", self.last_cmd)
        self.assertIn("--reaction", self.last_cmd)
        self.assertIn("love", self.last_cmd)
        # Numeric chat_id uses --chat-id
        self.assertIn("--chat-id", self.last_cmd)
        self.assertIn("42", self.last_cmd)

    async def test_react_rejects_non_numeric_chat_id(self):
        """imsg react's CLI only supports --chat-id (numeric), not --to.
        Non-numeric chat_id is an API error."""
        with self._patch_subprocess():
            result = await self.adapter.react(
                chat_id="+15555550100",  # handle, not int
                message_id="800",
                reaction="like",
            )
        self.assertFalse(result.success)
        self.assertIn("numeric chat_id", result.error)

    async def test_react_handles_acl_permission_error(self):
        """If imsg react fails with osascript permission error, the adapter
        returns a clear actionable error message."""
        stderr_text = (
            b'appleScriptFailure("osascript is not allowed to send keystrokes")'
        )
        with self._patch_subprocess(returncode=1, stderr=stderr_text):
            result = await self.adapter.react(
                chat_id="42", message_id="800", reaction="like"
            )
        self.assertFalse(result.success)
        self.assertIn("Accessibility", result.error)
        self.assertIn("System Settings", result.error)

    async def test_react_with_empty_chat_id_rejected(self):
        result = await self.adapter.react(chat_id="", message_id="1", reaction="like")
        self.assertFalse(result.success)
        self.assertIn("empty chat_id", result.error)

    async def test_react_with_empty_reaction_rejected(self):
        result = await self.adapter.react(chat_id="42", message_id="1", reaction="")
        self.assertFalse(result.success)
        self.assertIn("empty reaction", result.error)


# ---------------------------------------------------------------------------
# send_typing tests
# ---------------------------------------------------------------------------


class TestSendTyping(unittest.IsolatedAsyncioTestCase):
    """Test the send_typing / stop_typing paths — invoke imsg typing subprocess."""

    async def asyncSetUp(self):
        cfg = PlatformConfig(enabled=True, extra={})
        self.adapter = imsg.ImsgAdapter(cfg)
        self.adapter.cli_path = "/opt/homebrew/bin/imsg"
        # Pretend we're on macOS 14 so the SIP probe doesn't fire —
        # the typing path on macOS <= 15 is well-understood and the
        # tests focus on subprocess wiring, not platform detection.
        self.adapter._typing_disabled = False
        self.calls: list[list] = []
        # Force the macOS version check to return <=15 so we never
        # trigger the csrutil subprocess probe in unit tests.
        self._platform_patcher = patch(
            "platform.mac_ver", return_value=("14.5", ("", "", ""), "")
        )
        self._platform_patcher.start()

    async def asyncTearDown(self):
        self._platform_patcher.stop()

    def _patch_subprocess(self):
        async def fake_exec(*args, **kwargs):
            self.calls.append(list(args))

            proc = MagicMock()
            proc.returncode = 0

            async def fake_communicate(input=None):
                return b"", b""

            proc.communicate = fake_communicate
            return proc

        return patch("asyncio.create_subprocess_exec", side_effect=fake_exec)

    async def test_send_typing_invokes_imsg_typing(self):
        with self._patch_subprocess():
            await self.adapter.send_typing(chat_id="+155****0100")
        self.assertEqual(len(self.calls), 1)
        cmd = self.calls[0]
        self.assertIn("typing", cmd)
        self.assertIn("--to", cmd)
        self.assertIn("+155****0100", cmd)
        # --duration keeps the indicator visible briefly
        self.assertIn("--duration", cmd)

    async def test_send_typing_for_group_chat_uses_to_with_lookup(self):
        """When chat_id is a numeric rowid, we look up the chat_identifier
        in the messages DB and pass it via --to. (The CLI accepts either
        a phone number or an email in --to; chat_identifier from chat.db
        is in ``iMessage;-;+1...`` form which the CLI parses correctly.)
        """
        with (
            self._patch_subprocess(),
            patch.object(
                self.adapter,
                "_lookup_chat_identifier",
                return_value="iMessage;-;+155****0100",
            ),
        ):
            await self.adapter.send_typing(chat_id="42")
        # First call is the typing subprocess (csrutil not invoked on macOS < 26)
        typing_calls = [c for c in self.calls if "typing" in c]
        self.assertEqual(len(typing_calls), 1)
        cmd = typing_calls[0]
        self.assertIn("--to", cmd)
        self.assertIn("iMessage;-;+155****0100", cmd)
        # Did NOT fall back to --chat-id because the lookup succeeded.
        self.assertNotIn("--chat-id", cmd)

    async def test_send_typing_skips_on_macos_26_with_sip_enabled(self):
        """On macOS 26 (Tahoe), typing requires SIP disabled because the
        imagent daemon rejects third-party clients. We detect SIP once,
        cache the positive result, and skip subsequent attempts.
        """
        self.adapter._typing_disabled = False
        self.adapter._sip_status = None
        with patch.object(self.adapter, "_detect_sip_status", return_value=True):
            await self.adapter.send_typing(chat_id="+155****0100")
        typing_calls = [c for c in self.calls if "typing" in c]
        self.assertEqual(len(typing_calls), 0)
        self.assertTrue(self.adapter._typing_disabled)
        self.calls.clear()
        # Second call: cached skip, no probe, no subprocess
        await self.adapter.send_typing(chat_id="+155****0100")
        self.assertEqual(self.calls, [])

    async def test_send_typing_skips_on_macos_26_when_sip_disabled(self):
        """When SIP is disabled, the typing subprocess is still called —
        we don't block legitimate use.
        """
        self.adapter._typing_disabled = False
        self.adapter._sip_status = None
        with (
            patch.object(self.adapter, "_detect_sip_status", return_value=False),
            self._patch_subprocess(),
        ):
            await self.adapter.send_typing(chat_id="+155****0100")
        typing_calls = [c for c in self.calls if "typing" in c]
        self.assertEqual(len(typing_calls), 1)
        self.assertFalse(self.adapter._typing_disabled)

    async def test_detect_sip_status_probes_csrutil(self):
        """The SIP detector runs csrutil and returns True when enabled."""
        self.adapter._sip_status = None

        async def smart_exec(*args, **kwargs):
            self.calls.append(list(args))
            return await self._fake_csrutil_proc("enabled")

        with (
            patch("platform.mac_ver", return_value=("26.0", ("", "", ""), "")),
            patch("asyncio.create_subprocess_exec", side_effect=smart_exec),
        ):
            result = await self.adapter._detect_sip_status()
        self.assertEqual(
            result, True, f"expected True, got {result}; calls={self.calls}"
        )
        csrutil_calls = [c for c in self.calls if c and "csrutil" in c[0]]
        self.assertEqual(
            len(csrutil_calls), 1, f"expected 1 csrutil call, got {self.calls}"
        )
        # Cached: second call doesn't probe
        self.calls.clear()
        result2 = await self.adapter._detect_sip_status()
        self.assertEqual(result2, True)
        self.assertEqual(self.calls, [])

    async def test_detect_sip_status_skips_on_macos_under_26(self):
        """On macOS <=15, _detect_sip_status returns None without probing."""
        self.adapter._sip_status = None
        with (
            patch("platform.mac_ver", return_value=("14.5", ("", "", ""), "")),
            self._patch_subprocess(),
        ):
            result = await self.adapter._detect_sip_status()
        self.assertIsNone(result)
        # No csrutil call was made
        csrutil_calls = [c for c in self.calls if "csrutil" in c]
        self.assertEqual(len(csrutil_calls), 0)

    @staticmethod
    async def _fake_csrutil_proc(state):
        """Return a plain object with a real async communicate."""

        class _P:
            returncode = 0

            async def communicate(self):
                return (
                    f"System Integrity Protection status: {state}\n".encode(),
                    b"",
                )

        return _P()

    async def test_send_typing_falls_back_to_chat_id_when_to_fails(self):
        """If the rowid lookup returns empty and --to fails with a
        non-zero code, retry with --chat-id. The legacy macOS <=15 path
        uses --chat-id, which is more reliable for numeric rowids.
        """

        async def failing_exec(*args, **kwargs):
            self.calls.append(list(args))

            # Plain class with a real async communicate — MagicMock's
            # auto-attributes have caused flakes in this test.
            class _P:
                returncode = 1

                async def communicate(self):
                    return b"", b"imsg typing: chat not found"

            return _P()

        with patch("asyncio.create_subprocess_exec", side_effect=failing_exec):
            await self.adapter.send_typing(chat_id="42")
        # First call: --to 42 (lookup returned empty, so the rowid is used as --to)
        # Second call: --chat-id 42 (fallback)
        self.assertEqual(len(self.calls), 2)
        first = self.calls[0]
        self.assertIn("--to", first)
        self.assertIn("42", first)
        second = self.calls[1]
        self.assertIn("--chat-id", second)
        self.assertIn("42", second)

    async def test_send_typing_swallows_errors(self):
        """Typing failures are non-fatal. They must not raise."""

        async def broken_exec(*args, **kwargs):
            raise OSError("imsg typing not available")

        with patch("asyncio.create_subprocess_exec", side_effect=broken_exec):
            # Should not raise
            await self.adapter.send_typing(chat_id="+15555550100")

    async def test_send_typing_empty_chat_id_is_noop(self):
        with self._patch_subprocess():
            await self.adapter.send_typing(chat_id="")
        # No subprocess calls
        self.assertEqual(self.calls, [])

    async def test_stop_typing_invokes_typing_stop(self):
        with self._patch_subprocess():
            await self.adapter.stop_typing(chat_id="+15555550100")
        self.assertEqual(len(self.calls), 1)
        cmd = self.calls[0]
        self.assertIn("typing", cmd)
        self.assertIn("--stop", cmd)
        self.assertIn("true", cmd)


# ---------------------------------------------------------------------------
# Patch tests (unchanged from v1)
# ---------------------------------------------------------------------------


class TestAuthzPatch(unittest.TestCase):
    """The bundled patch must apply cleanly and produce the post-IMSG state.

    Uses a checked-in fixture of the pre-IMSG authz_mixin.py (see
    tests/fixtures/authz_mixin_prepatch.py) so this runs in CI without
    a live Hermes install.  The install-dry-run test below is the one
    that needs the real Hermes tree and is gated separately.
    """

    def test_patch_applies_to_clean_tree(self):
        """Apply the patch to a freshly checked-out authz_mixin.py and
        verify the result is byte-identical to the post-patch fixture."""
        with tempfile.TemporaryDirectory() as work_str:
            work = Path(work_str)
            (work / "gateway").mkdir()
            shutil.copy(PRE_PATCH_AUTHZ_PATH, work / "gateway" / "authz_mixin.py")
            shutil.copy(PATCH_PATH, work / "authz.patch")

            result = subprocess.run(
                ["patch", "-p1", "-d", str(work), "-i", "authz.patch"],
                capture_output=True,
                text=True,
            )
            self.assertEqual(result.returncode, 0, f"patch failed: {result.stderr}")
            self.assertIn("patching file", result.stdout)

            applied = (work / "gateway" / "authz_mixin.py").read_text()
            expected = POST_PATCH_AUTHZ_PATH.read_text()
            self.assertEqual(
                applied,
                expected,
                "patched file does not match post-patch fixture — "
                "regenerate tests/fixtures/authz_mixin_postpatch.py AND "
                "patches/authz.patch from the same source",
            )

    def test_patch_is_idempotent(self):
        """Re-applying the patch with --forward --batch must refuse cleanly.

        Without those flags, GNU patch will detect the reversed direction
        and SILENTLY APPLY IN REVERSE, removing the lines. That's the
        worst possible failure mode for an install script — and the bug
        we caught while writing these tests. The README install steps
        must use ``patch -p1 --forward --batch``.
        """
        post = POST_PATCH_AUTHZ_PATH.read_text()
        with tempfile.TemporaryDirectory() as work_str:
            work = Path(work_str)
            (work / "gateway").mkdir()
            (work / "gateway" / "authz_mixin.py").write_text(post)
            shutil.copy(PATCH_PATH, work / "authz.patch")

            result = subprocess.run(
                [
                    "patch",
                    "-p1",
                    "-d",
                    str(work),
                    "-i",
                    "authz.patch",
                    "--forward",
                    "--batch",
                ],
                capture_output=True,
                text=True,
            )
            self.assertNotEqual(
                result.returncode,
                0,
                "patch --forward --batch should refuse to re-apply",
            )
            self.assertIn("Ignoring", result.stdout)

            after = (work / "gateway" / "authz_mixin.py").read_text()
            self.assertEqual(
                after,
                post,
                "file changed after rejected re-apply — patch is not safe",
            )


class TestInstallDryRun(unittest.TestCase):
    """Simulate the README's install steps in a temp HERMES_HOME.

    Runs in CI by copying the stub ``gateway`` package into the temp
    tree so the shipped adapter can ``import gateway.config`` without
    needing the live Hermes checkout.  The point of this test is to
    verify the SHIPPED adapter file (not the local development one)
    imports cleanly when dropped into a hermes-agent/gateway/platforms/
    directory — the stub package is a stand-in for the real one.
    """

    def test_full_install_recipe(self):
        with tempfile.TemporaryDirectory() as work_str:
            work = Path(work_str)
            hermes = work / "hermes-agent"
            (hermes / "gateway" / "platforms").mkdir(parents=True)

            # Seed the temp Hermes with the post-patch authz_mixin state
            # and the stub gateway/ package so the adapter can import.
            shutil.copy(POST_PATCH_AUTHZ_PATH, hermes / "gateway" / "authz_mixin.py")
            shutil.copy(ADAPTER_PATH, hermes / "gateway" / "platforms" / "imsg.py")
            # The stub gateway.config / gateway.platforms.base provide the
            # same surface as the real ones for the symbols the adapter
            # imports; that is what makes the dry-run viable on CI.
            stub_gateway_src = REPO_ROOT / "tests" / "_stubs" / "gateway"
            for sub in ("config.py", "__init__.py", "session.py"):
                shutil.copy(stub_gateway_src / sub, hermes / "gateway" / sub)
            shutil.copytree(
                stub_gateway_src / "platforms",
                hermes / "gateway" / "platforms",
                dirs_exist_ok=True,
            )

            sys.path.insert(0, str(hermes))
            try:
                from gateway.platforms import imsg as fresh_imsg
                from gateway.config import Platform

                self.assertTrue(hasattr(fresh_imsg, "ImsgAdapter"))
                self.assertTrue(hasattr(fresh_imsg, "check_imsg_requirements"))
                self.assertTrue(hasattr(fresh_imsg, "render_imessage"))
                self.assertEqual(fresh_imsg.ImsgAdapter.platform, Platform.IMSG)
                self.assertTrue(fresh_imsg.check_imsg_requirements())
            finally:
                sys.path.remove(str(hermes))


if __name__ == "__main__":
    unittest.main(verbosity=2)
