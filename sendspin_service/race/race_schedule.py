"""Four service-loop countdown callbacks for RotorHazard's planned race start."""

from __future__ import annotations

import asyncio
import time
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Callable

DEFAULT_THRESHOLDS = (60, 30, 10, 5)
DEFAULT_MIN_TIMER_DELAY_SEC = 0.25


class RaceSchedule:
    """Keep one schedule, with no threads, overdue replay or duplicate thresholds."""

    def __init__(self, emit: Callable[[int, float], None]) -> None:
        """Defer timers until state and a fresh clock mapping are available."""
        self._emit = emit
        self._key: tuple | None = None
        self._revision = 0
        self._fired: set[int] = set()
        self._handles: list[asyncio.TimerHandle] = []

    def update(self, key: tuple, start: float, offset: float) -> None:
        """Refresh clock alignment without replaying an already handled threshold."""
        self._revision += 1
        for handle in self._handles:
            handle.cancel()
        self._handles.clear()
        new_schedule = key != self._key
        if new_schedule:
            self._key = key
            self._fired.clear()
        now = time.monotonic()
        loop = asyncio.get_running_loop()
        for seconds in DEFAULT_THRESHOLDS:
            if seconds in self._fired:
                continue
            target = start - seconds
            delay = target + offset - now
            minimum = DEFAULT_MIN_TIMER_DELAY_SEC if new_schedule else 0
            if delay <= minimum:
                self._fired.add(seconds)
                continue
            self._handles.append(
                loop.call_later(delay, self._fire, self._revision, seconds, target)
            )

    def _fire(self, revision: int, seconds: int, target: float) -> None:
        if revision != self._revision or seconds in self._fired:
            return
        self._fired.add(seconds)
        self._emit(seconds, target)

    def cancel(self) -> None:
        """Fence even a callback already taken from the loop's timer queue."""
        self._revision += 1
        for handle in self._handles:
            handle.cancel()
        self._handles.clear()
        self._key = None
        self._fired.clear()
