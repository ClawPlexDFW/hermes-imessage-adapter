"""Stub for ``gateway.session.SessionSource`` — used by the imsg adapter to
attach session-routing metadata to inbound messages.

The full ``SessionSource`` dataclass in the live Hermes install carries
~15 fields and a ``to_dict()`` method.  The imsg adapter only sets
nine of them, so this stub mirrors those and uses ``field(default=...)``
for the rest.  ``description`` is kept because the imsg adapter does
NOT call it during tests, but live adapters sometimes read it for log
formatting and we want ``isinstance`` checks in tests to behave
naturally."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Optional


@dataclass
class SessionSource:
    platform: Any  # Platform enum from gateway.config
    chat_id: str
    chat_name: Optional[str] = None
    chat_type: str = "dm"
    user_id: Optional[str] = None
    user_name: Optional[str] = None
    thread_id: Optional[str] = None
    chat_topic: Optional[str] = None
    user_id_alt: Optional[str] = None
    chat_id_alt: Optional[str] = None
    is_bot: bool = False
    guild_id: Optional[str] = None
    parent_chat_id: Optional[str] = None
    message_id: Optional[str] = None
    role_authorized: bool = False
