"""Async audio job queue with priority and expiry."""

from __future__ import annotations

import heapq
import logging
import threading
import time
from dataclasses import dataclass, field
from enum import IntEnum
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Callable

logger = logging.getLogger(__name__)

# Default expiry time for a job
DEFAULT_EXPIRY_SEC = 5.0


class Priority(IntEnum):
    """Job priority — lower value = higher priority."""

    SIGNAL = -1  # time-critical race tones
    HIGH = 0  # winner, interrupt messages
    NORMAL = 1  # general announcements, pilot done
    LOW = 2  # crossing beeps
    LAP = 3  # lap-time speech always yields to other announcements


@dataclass(order=True)
class AudioJob:
    """A single audio playback job: one or more WAV files played in sequence."""

    priority: Priority
    expires_at: float
    text: str = field(compare=False)
    wav_items: list[WavItem] = field(compare=False)
    play_at: float | None = field(compare=False, default=None)
    volume: float = field(compare=False, default=1.0)
    cancelled: threading.Event = field(compare=False, default_factory=threading.Event)


@dataclass(frozen=True)
class WavItem:
    """A WAV clip supplied either as a path or inline bytes."""

    name: str
    path: str | None = None
    data: bytes | None = None


class AudioQueue:
    """Priority queue with a single daemon worker thread.

    The worker drains jobs in priority order, dropping any that have exceeded
    their expiry time. Each ready job is handed to *player* with its deadline
    so the output backend can avoid scheduling stale audio.
    """

    def __init__(
        self,
        player: Callable[..., None],
        interrupt: Callable[[], None] | None = None,
    ) -> None:
        """Start one worker; signals can cancel active speech from the producer."""
        self._player = player
        self._interrupt = interrupt
        self._condition = threading.Condition()
        self._jobs: list[AudioJob] = []
        self._active: AudioJob | None = None
        self._last_priority: Priority | None = None
        self._thread = threading.Thread(
            target=self._worker, daemon=True, name="sendspin-service-audio"
        )
        self._thread.start()

    def enqueue(  # noqa: PLR0913
        self,
        text: str,
        wav_items: list[WavItem],
        priority: Priority = Priority.NORMAL,
        expiry_sec: float = DEFAULT_EXPIRY_SEC,
        play_at: float | None = None,
        volume: float = 1.0,
    ) -> None:
        """Interrupt lower-priority speech when a fresh race signal arrives."""
        job = AudioJob(
            priority=priority,
            expires_at=time.monotonic() + expiry_sec,
            text=text,
            wav_items=wav_items,
            play_at=play_at,
            volume=volume,
        )
        with self._condition:
            if time.monotonic() >= job.expires_at:
                return
            if priority == Priority.SIGNAL:
                self._jobs = [j for j in self._jobs if j.priority != Priority.LAP]
                heapq.heapify(self._jobs)
            preempts = (
                self._last_priority == Priority.LAP and priority < Priority.LAP
            ) or (
                priority == Priority.SIGNAL
                and self._last_priority not in (None, Priority.SIGNAL)
            )
            if preempts:
                if self._active is not None:
                    self._active.cancelled.set()
                if self._interrupt is not None:
                    self._interrupt()
                self._last_priority = priority
            heapq.heappush(self._jobs, job)
            self._condition.notify()

    def clear(self) -> int:
        """Invalidate active work and discard pending jobs."""
        with self._condition:
            count = len(self._jobs)
            self._jobs.clear()
            if self._active is not None:
                self._active.cancelled.set()
            self._last_priority = None
            return count

    def _worker(self) -> None:
        """Dispatch jobs serially while allowing cancellation during playback."""
        while True:
            with self._condition:
                self._condition.wait_for(lambda: bool(self._jobs))
                job = heapq.heappop(self._jobs)
                if time.monotonic() > job.expires_at:
                    continue
                self._active = job
                self._last_priority = job.priority
            try:
                self._player(
                    job.wav_items,
                    job.expires_at,
                    job.play_at,
                    job.volume,
                    cancelled=job.cancelled,
                )
            except Exception:
                logger.exception("Sendspin service worker error for '%s'", job.text)
            finally:
                with self._condition:
                    self._active = None
