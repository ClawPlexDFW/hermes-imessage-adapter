"""Tests for the imsg-tools plugin (agent-callable imsg_react / imsg_send_file)."""

from __future__ import annotations

import importlib.util
import json
import sys
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

# Make the plugin importable
PLUGIN_PATH = Path(__file__).resolve().parent.parent / "plugins" / "imsg-tools"
sys.path.insert(0, str(PLUGIN_PATH.parent))
sys.path.insert(0, str(PLUGIN_PATH))

# Import the plugin module fresh
_spec = importlib.util.spec_from_file_location(  # noqa: E402
    "imsg_tools", str(PLUGIN_PATH / "__init__.py")
)
imsg_tools = importlib.util.module_from_spec(_spec)  # noqa: E402
_spec.loader.exec_module(imsg_tools)  # noqa: E402


class TestImsgReactSchema(unittest.TestCase):
    """Tool schema is valid and includes the right keys."""

    def test_schema_required_fields(self):
        schema = imsg_tools.IMSG_REACT_SCHEMA
        self.assertEqual(schema["name"], "imsg_react")
        self.assertIn("description", schema)
        params = schema["parameters"]
        self.assertEqual(set(params["required"]), {"chat_id", "reaction"})
        self.assertIn("chat_id", params["properties"])
        self.assertIn("reaction", params["properties"])

    def test_reaction_enum(self):
        enum = imsg_tools.IMSG_REACT_SCHEMA["parameters"]["properties"]["reaction"][
            "enum"
        ]
        self.assertEqual(
            set(enum),
            {"love", "like", "dislike", "laugh", "emphasis", "question"},
        )


class TestImsgSendFileSchema(unittest.TestCase):
    def test_schema_required_fields(self):
        schema = imsg_tools.IMSG_SEND_FILE_SCHEMA
        self.assertEqual(schema["name"], "imsg_send_file")
        params = schema["parameters"]
        self.assertEqual(set(params["required"]), {"chat_id", "file_path"})

    def test_caption_is_optional(self):
        params = imsg_tools.IMSG_SEND_FILE_SCHEMA["parameters"]
        self.assertNotIn("caption", params["required"])


def _make_capture(calls, communicate_return=(b"sent\n", b"")):
    """Build a fake ``asyncio.create_subprocess_exec`` that records calls.

    Returns a function suitable for ``side_effect=`` on ``patch``. The
    returned function returns a plain object with a real async
    ``communicate()`` — MagicMock's auto-attributes shadow instance
    assignments and have caused flakes in similar tests.
    """

    def _capture(*args, **kwargs):
        calls.append(list(args))
        proc = MagicMock()
        proc.returncode = 0

        async def communicate():
            return communicate_return

        proc.communicate = communicate
        return proc

    return _capture


class TestHandleImsgReact(unittest.IsolatedAsyncioTestCase):
    """The handler dispatches ``imsg react --chat-id <rowid> --reaction <r>``.

    Per the upstream ``imsg`` CLI v0.11.1, ``react`` only accepts
    ``--chat-id`` (rowid integer) and ``--reaction``. It cannot target a
    specific message — it always reacts to the most recent incoming
    message in the chat. There is no ``--to`` or ``--chat-identifier``
    flag for ``react``.

    When the agent passes a non-numeric ``chat_id`` (e.g. a chat
    identifier or phone number), the handler looks up the rowid in the
    adapter's module-level ``CHAT_ROWID_BY_IDENTIFIER`` cache. If the
    cache has the mapping, the rowid is used. If not, the handler
    returns a clear error explaining that the agent must pass the
    integer rowid or wait for an inbound message to populate the cache.
    """

    async def asyncSetUp(self):
        self.calls: list = []
        # Reset the module-level cache to a known empty state so each
        # test starts fresh. We import lazily because the adapter may
        # not be on sys.path in all test contexts.
        from gateway.platforms import imsg as _imsg  # type: ignore
        _imsg.CHAT_ROWID_BY_IDENTIFIER.clear()

    async def test_react_with_numeric_chat_id_uses_chat_id_flag(self):
        with patch(
            "asyncio.create_subprocess_exec",
            side_effect=_make_capture(self.calls),
        ):
            result = await imsg_tools._handle_imsg_react(
                {"chat_id": "4", "reaction": "love"}
            )
        parsed = json.loads(result)
        self.assertTrue(parsed["ok"])
        self.assertEqual(parsed["chat_id"], "4")
        self.assertEqual(parsed["reaction"], "love")
        cmd = self.calls[0]
        self.assertEqual(cmd[1], "react")
        self.assertIn("--chat-id", cmd)
        self.assertIn("4", cmd)
        # imsg react has no --to or --chat-identifier flag.
        self.assertNotIn("--to", cmd)
        self.assertNotIn("--chat-identifier", cmd)

    async def test_react_with_chat_identifier_resolves_via_cache(self):
        # Populate the cache as if a prior inbound message from this
        # chat_identifier mapped to rowid 4.
        from gateway.platforms import imsg as _imsg  # type: ignore
        _imsg.CHAT_ROWID_BY_IDENTIFIER["iMessage;-;+194****2639"] = 4

        with patch(
            "asyncio.create_subprocess_exec",
            side_effect=_make_capture(self.calls),
        ):
            result = await imsg_tools._handle_imsg_react(
                {
                    "chat_id": "iMessage;-;+194****2639",
                    "reaction": "laugh",
                }
            )
        parsed = json.loads(result)
        self.assertTrue(parsed["ok"])
        # The handler must have resolved the identifier to rowid 4 and
        # used --chat-id, not the (nonexistent) --chat-identifier flag.
        cmd = self.calls[0]
        self.assertIn("--chat-id", cmd)
        self.assertIn("4", cmd)
        self.assertNotIn("--chat-identifier", cmd)
        self.assertNotIn("iMessage;-;+194****2639", cmd)

    async def test_react_with_phone_number_falls_back_to_cache(self):
        # Same as above: a phone number passed as chat_id is treated as
        # a non-numeric identifier; the rowid cache resolves it.
        from gateway.platforms import imsg as _imsg  # type: ignore
        _imsg.CHAT_ROWID_BY_IDENTIFIER["+194****2639"] = 4

        with patch(
            "asyncio.create_subprocess_exec",
            side_effect=_make_capture(self.calls),
        ):
            await imsg_tools._handle_imsg_react(
                {
                    "chat_id": "+194****2639",
                    "reaction": "like",
                }
            )
        cmd = self.calls[0]
        # imsg react has no --to flag — the rowid must always be used.
        self.assertNotIn("--to", cmd)
        self.assertIn("--chat-id", cmd)
        self.assertIn("4", cmd)

    async def test_react_with_unknown_identifier_returns_error(self):
        # Cache is empty (set in asyncSetUp), so a non-numeric chat_id
        # has nowhere to resolve to. The handler must return a clear
        # error rather than calling imsg with an invalid flag.
        result = await imsg_tools._handle_imsg_react(
            {
                "chat_id": "iMessage;-;+194****2639",
                "reaction": "love",
            }
        )
        parsed = json.loads(result)
        self.assertFalse(parsed["ok"])
        self.assertIn("numeric chat_id", parsed["error"])
        self.assertIn("rowid", parsed["error"])
        # No subprocess call was made.
        self.assertEqual(self.calls, [])

    async def test_react_with_unknown_reaction_returns_error(self):
        with patch(
            "asyncio.create_subprocess_exec",
            side_effect=_make_capture(self.calls),
        ):
            result = await imsg_tools._handle_imsg_react(
                {"chat_id": "4", "reaction": "bogus"}
            )
        parsed = json.loads(result)
        self.assertFalse(parsed["ok"])
        self.assertIn("unknown reaction", parsed["error"])
        # No subprocess call
        self.assertEqual(self.calls, [])

    async def test_react_with_empty_chat_id_returns_error(self):
        result = await imsg_tools._handle_imsg_react(
            {"chat_id": "", "reaction": "love"}
        )
        parsed = json.loads(result)
        self.assertFalse(parsed["ok"])
        self.assertIn("chat_id is required", parsed["error"])

    async def test_react_failure_returns_ok_false(self):
        def _failing(*args, **kwargs):
            self.calls.append(list(args))
            proc = MagicMock()
            proc.returncode = 1

            async def communicate():
                return b"", b"chat not found"

            proc.communicate = communicate
            return proc

        with patch("asyncio.create_subprocess_exec", side_effect=_failing):
            result = await imsg_tools._handle_imsg_react(
                {"chat_id": "4", "reaction": "love"}
            )
        parsed = json.loads(result)
        self.assertFalse(parsed["ok"])


class TestHandleImsgSendFile(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.calls: list = []
        import tempfile

        self.tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".txt", mode="w")
        self.tmp.write("hello imsg-tools test")
        self.tmp.close()
        self.tmp_path = self.tmp.name

    async def asyncTearDown(self):
        Path(self.tmp_path).unlink(missing_ok=True)

    async def test_send_file_with_caption(self):
        with patch(
            "asyncio.create_subprocess_exec",
            side_effect=_make_capture(self.calls),
        ):
            result = await imsg_tools._handle_imsg_send_file(
                {
                    "chat_id": "+194****2639",
                    "file_path": self.tmp_path,
                    "caption": "look at this",
                }
            )
        parsed = json.loads(result)
        self.assertTrue(parsed["ok"])
        # macOS resolves /var/folders -> /private/var/folders. Use
        # Path comparison so the test passes on both macOS and Linux.
        expected = Path(self.tmp_path).resolve()
        cmd = self.calls[0]
        self.assertEqual(cmd[1], "send")
        self.assertIn("--file", cmd)
        self.assertTrue(
            any(Path(arg).resolve() == expected for arg in cmd if arg != "--file"),
            f"file path {expected} not in cmd {cmd}",
        )
        self.assertIn("--text", cmd)
        self.assertIn("look at this", cmd)

    async def test_send_file_without_caption(self):
        with patch(
            "asyncio.create_subprocess_exec",
            side_effect=_make_capture(self.calls),
        ):
            result = await imsg_tools._handle_imsg_send_file(
                {
                    "chat_id": "4",
                    "file_path": self.tmp_path,
                }
            )
        parsed = json.loads(result)
        self.assertTrue(parsed["ok"])
        cmd = self.calls[0]
        self.assertNotIn("--text", cmd)

    async def test_send_file_missing_file_returns_error(self):
        result = await imsg_tools._handle_imsg_send_file(
            {
                "chat_id": "4",
                "file_path": "/nonexistent/file.txt",
            }
        )
        parsed = json.loads(result)
        self.assertFalse(parsed["ok"])
        self.assertIn("file not found", parsed["error"])

    async def test_send_file_empty_chat_id_returns_error(self):
        result = await imsg_tools._handle_imsg_send_file(
            {
                "chat_id": "",
                "file_path": self.tmp_path,
            }
        )
        parsed = json.loads(result)
        self.assertFalse(parsed["ok"])


class TestPluginRegister(unittest.IsolatedAsyncioTestCase):
    """The ``register(ctx)`` function wires both tools to the plugin
    context so they appear in the agent's tool schema.
    """

    async def test_register_registers_both_tools(self):
        ctx = MagicMock()
        ctx.register_tool = MagicMock()
        imsg_tools.register(ctx)
        registered_names = [
            call.kwargs["name"] for call in ctx.register_tool.call_args_list
        ]
        self.assertIn("imsg_react", registered_names)
        self.assertIn("imsg_send_file", registered_names)
        # Both registered on the `imsg` toolset
        for call in ctx.register_tool.call_args_list:
            self.assertEqual(call.kwargs["toolset"], "imsg")
            self.assertTrue(call.kwargs["is_async"])


if __name__ == "__main__":
    unittest.main()
