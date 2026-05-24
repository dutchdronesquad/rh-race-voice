"""Race schedule countdown callout timers."""

from __future__ import annotations

import logging
import threading
import time
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence

logger = logging.getLogger(__name__)

DEFAULT_THRESHOLDS = (60, 30, 10, 5)
DEFAULT_MIN_TIMER_DELAY_SEC = 0.25
PRECACHE_DIR_NAME = "schedule"
PRECACHE_SUBDIR = f"precache/{PRECACHE_DIR_NAME}"


class ScheduleCalloutManager:
    """Own race schedule countdown timers and stale timer cancellation."""

    def __init__(
        self,
        *,
        enqueue_callout: Callable[[str, Any], None],
        phrase_for: Callable[[int, Any], str],
        thresholds: Sequence[int] = DEFAULT_THRESHOLDS,
        min_timer_delay_sec: float = DEFAULT_MIN_TIMER_DELAY_SEC,
    ) -> None:
        """Initialize callbacks and countdown timing configuration."""
        self._enqueue_callout = enqueue_callout
        self._phrase_for = phrase_for
        self._thresholds = tuple(thresholds)
        self._min_timer_delay_sec = min_timer_delay_sec
        self._timers: list[threading.Timer] = []
        self._generation = 0
        self._lock = threading.Lock()

    def schedule(self, scheduled_at: float, settings: Any) -> None:
        """Replace pending countdown timers for one scheduled race."""
        seconds_remaining = scheduled_at - time.monotonic()
        new_timers: list[threading.Timer] = []
        with self._lock:
            self._generation += 1
            generation = self._generation

        for threshold in self._thresholds:
            delay_sec = seconds_remaining - threshold
            if delay_sec > self._min_timer_delay_sec:
                timer = threading.Timer(
                    delay_sec,
                    self._fire,
                    args=(self._phrase_for(threshold, settings), settings, generation),
                )
                timer.daemon = True
                timer.start()
                new_timers.append(timer)

        with self._lock:
            for timer in self._timers:
                timer.cancel()
            self._timers = new_timers

    def cancel(self) -> None:
        """Cancel all pending countdown timers."""
        with self._lock:
            self._generation += 1
            for timer in self._timers:
                timer.cancel()
            self._timers.clear()

    def _fire(self, phrase: str, settings: Any, generation: int) -> None:
        with self._lock:
            if generation != self._generation:
                return
        logger.debug("Race Voice firing schedule callout: %s", phrase)
        self._enqueue_callout(phrase, settings)
