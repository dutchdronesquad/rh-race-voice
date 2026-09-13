"""Race voice callouts for RotorHazard."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .event_adapter import RaceEventAdapter


def initialize(rhapi: Any) -> RaceEventAdapter:
    """Initialize the v2 event adapter; synthesis belongs to the voice service."""
    # Shared worker modules must remain importable without RotorHazard modules.
    from .event_adapter import RaceEventAdapter  # noqa: PLC0415

    return RaceEventAdapter(rhapi)
