"""Shared playback priority and WAV item types."""

from __future__ import annotations

from dataclasses import dataclass
from enum import IntEnum


class Priority(IntEnum):
    """Job priority — lower value = higher priority."""

    SIGNAL = -1  # time-critical race tones
    HIGH = 0  # winner, interrupt messages
    NORMAL = 1  # general announcements, pilot done
    LOW = 2  # crossing beeps
    LAP = 3  # lap-time speech always yields to other announcements


@dataclass(frozen=True)
class WavItem:
    """A WAV clip supplied either as a path or inline bytes."""

    name: str
    path: str | None = None
    data: bytes | None = None
