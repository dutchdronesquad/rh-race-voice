"""Race voice callouts for RotorHazard."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .plugin import RaceVoicePlugin


def initialize(rhapi: Any) -> RaceVoicePlugin:
    """RotorHazard plugin entry point."""
    # Allow the standalone worker to reuse Piper without importing RH modules.
    # Runtime dependencies still fail normally when RH initializes the plugin.
    from .plugin import RaceVoicePlugin  # noqa: PLC0415

    return RaceVoicePlugin(rhapi)
