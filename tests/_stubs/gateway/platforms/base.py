"""Minimal stub mirroring the Hermes platform base surface the imsg adapter uses.

The live ``gateway.platforms.base`` is 200KB+ and depends on
``utils.normalize_proxy_url`` and a long list of optional helpers.  The
adapter only needs four symbols from it, all listed below.  We mirror the
``@dataclass`` field shape and the ``BasePlatformAdapter.__init__``
signature so ``super().__init__(config, platform)`` works without
requiring every method the live base class provides.

Anything more elaborate — message-handler wiring, authorization
plumbing, fatal-error machinery — lives in the live base class and is
NOT needed for the unit tests.  If a future test needs one of those
helpers, extend this stub with a single minimal implementation rather
than pulling in the live module.
"""

from __future__ import annotations

import logging
from abc import ABC
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any, Callable, List, Optional

logger = logging.getLogger(__name__)


class MessageType(Enum):
    """Types of incoming messages — matches the live Hermes enum subset the adapter uses."""

    TEXT = "text"
    LOCATION = "location"
    PHOTO = "photo"
    VIDEO = "video"
    AUDIO = "audio"
    VOICE = "voice"
    DOCUMENT = "document"
    STICKER = "sticker"
    COMMAND = "command"


@dataclass
class MessageEvent:
    """Incoming message from a platform.

    Field list mirrors the live ``MessageEvent`` shape but only the
    fields the imsg adapter sets on the dataclass.  ``is_command`` and
    ``get_command`` are kept because the ImsgAdapter path never invokes
    them but the live parent class does in some helper methods we
    intentionally do not exercise here."""

    text: str
    message_type: MessageType = MessageType.TEXT
    source: Any = None
    raw_message: Any = None
    message_id: Optional[str] = None
    platform_update_id: Optional[int] = None
    media_urls: List[str] = field(default_factory=list)
    media_types: List[str] = field(default_factory=list)
    reply_to_message_id: Optional[str] = None
    reply_to_text: Optional[str] = None
    auto_skill: Optional[str] = None
    channel_prompt: Optional[str] = None
    channel_context: Optional[str] = None
    internal: bool = False
    timestamp: datetime = field(default_factory=datetime.now)

    def is_command(self) -> bool:
        return bool(self.text) and self.text.startswith("/")

    def get_command(self) -> Optional[str]:
        if not self.is_command():
            return None
        parts = self.text.split(maxsplit=1)
        raw = parts[0][1:].lower() if parts else None
        if raw and "@" in raw:
            raw = raw.split("@", 1)[0]
        if raw and "/" in raw:
            return None
        return raw


@dataclass
class SendResult:
    """Result of sending a message."""

    success: bool
    message_id: Optional[str] = None
    error: Optional[str] = None
    raw_response: Any = None


class BasePlatformAdapter(ABC):
    """Base class for platform adapters — stub for the imsg adapter to inherit.

    Mirrors only ``__init__`` and the small number of helpers the imsg
    adapter calls during testing.  Concrete method bodies are no-ops or
    raise ``NotImplementedError`` exactly as the live class would; tests
    that need different behavior stub them out with ``unittest.mock``."""

    supports_code_blocks: bool = False
    typed_command_prefix: str = "/"

    def __init__(self, config, platform):
        self.config = config
        self.platform = platform
        self._message_handler: Optional[Callable[..., Any]] = None
        self._running = False
        self._fatal_error_code: Optional[str] = None
        self._fatal_error_message: Optional[str] = None
        self._fatal_error_retryable = True
        self._fatal_error_handler: Optional[Callable[..., Any]] = None

    async def connect(self) -> None:  # pragma: no cover — overridden by ImsgAdapter
        raise NotImplementedError

    async def disconnect(self) -> None:  # pragma: no cover — overridden by ImsgAdapter
        raise NotImplementedError
