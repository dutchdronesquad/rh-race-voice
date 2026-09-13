"""Race voice callouts for RotorHazard."""

from __future__ import annotations

import os
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .event_adapter import RaceEventAdapter
    from .plugin import RaceVoicePlugin


def initialize(rhapi: Any) -> RaceVoicePlugin | RaceEventAdapter:
    """RotorHazard plugin entry point."""
    if os.environ.get("RACE_VOICE_EXPERIMENTAL_EVENTS") == "1":
        from .event_adapter import RaceEventAdapter  # noqa: PLC0415

        return RaceEventAdapter(rhapi)
    # Allow the standalone worker to reuse Piper without importing RH modules.
    # Runtime dependencies still fail normally when RH initializes the plugin.
    from .plugin import RaceVoicePlugin  # noqa: PLC0415

    return RaceVoicePlugin(rhapi)
