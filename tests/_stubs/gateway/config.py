"""Minimal stub mirroring only the Hermes gateway config surface the imsg adapter uses."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, Optional


class Platform(Enum):
    """Platform identifier — mirrors the live Hermes enum, but only carries
    the IMSG member the adapter cares about.  Tests assert against
    ``Platform.IMSG`` directly; if a test ever needs another member, add
    it here."""

    IMSG = "imsg"


class HomeChannel:
    """Stub: tests never construct one, but PlatformConfig references it."""

    def __init__(self, *args, **kwargs):
        self.args = args
        self.kwargs = kwargs


@dataclass
class PlatformConfig:
    """Configuration for a single messaging platform.

    Mirrors the live ``gateway.config.PlatformConfig`` shape closely enough
    that ``PlatformConfig(enabled=True, extra={...})`` and
    ``PlatformConfig(enabled=True, token=...)`` work the way the adapter
    expects.  Fields the live config has but the adapter does not touch
    (e.g. ``reply_to_mode``) are intentionally omitted — keep the stub
    narrow so test failures point at the adapter, not at drift in the
    full live config schema."""

    enabled: bool = False
    token: Optional[str] = None
    api_key: Optional[str] = None
    home_channel: Optional[HomeChannel] = None
    extra: Dict[str, Any] = field(default_factory=dict)
