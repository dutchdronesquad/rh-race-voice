"""Async audio job queue with priority and expiry."""

from __future__ import annotations

import logging
import queue
import threading
import time
from dataclasses import dataclass, field
from enum import IntEnum
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

logger = logging.getLogger(__name__)

# Default expiry time for a job
DEFAULT_EXPIRY_SEC = 5.0


class Priority(IntEnum):
    """Job priority — lower value = higher priority."""

    HIGH = 0  # winner, interrupt messages
    NORMAL = 1  # lap callouts, pilot done
    LOW = 2  # crossing beeps
    CONTROL = -2  # serialized stop after an in-flight upload


@dataclass(order=True)
class AudioJob:
    """A single audio playback job: one or more WAV files played in sequence."""

    priority: Priority
    expires_at: float
    text: str = field(compare=False)
    wav_paths: list[Path] = field(compare=False)
    play_at: float | None = field(compare=False, default=None)
    volume: float = field(compare=False, default=1.0)
    generation: int = field(compare=False, default=0)
    stop_player: Callable[[], None] | None = field(compare=False, default=None)


class AudioQueue:
    """Priority queue with a single daemon worker thread.

    The worker drains jobs in priority order, dropping any that have exceeded
    their expiry time. Each ready job is handed to *player* with its deadline
    so the output backend can avoid scheduling stale audio.
    """

    def __init__(
        self,
        player: Callable[
            [str, list[Path], Priority, float | None, float | None, float],
            None,
        ],
    ) -> None:
        """Start the background worker thread."""
        self._player = player
        self._generation = 0
        self._lock = threading.RLock()
        self._queue: queue.PriorityQueue[AudioJob] = queue.PriorityQueue()
        self._thread = threading.Thread(
            target=self._worker, daemon=True, name="race_voice_audio"
        )
        self._thread.start()

    def enqueue(  # noqa: PLR0913
        self,
        text: str,
        wav_paths: list[Path],
        priority: Priority = Priority.NORMAL,
        expiry_sec: float = DEFAULT_EXPIRY_SEC,
        play_at: float | None = None,
        volume: float = 1.0,
    ) -> None:
        """Add a job to the queue. Returns immediately."""
        with self._lock:
            job = AudioJob(
                priority=priority,
                generation=self._generation,
                expires_at=time.monotonic() + expiry_sec,
                text=text,
                wav_paths=wav_paths,
                play_at=play_at,
                volume=volume,
            )
            self._queue.put(job)
            logger.debug("Race Voice queued [%s] '%s'", priority.name, text)

    def clear(self) -> int:
        """Drop queued jobs that have not started yet."""
        with self._lock:
            self._generation += 1
            count = 0
            controls = []
            while True:
                try:
                    job = self._queue.get_nowait()
                except queue.Empty:
                    break
                if job.stop_player is not None:
                    controls.append(job)
                else:
                    count += 1
                self._queue.task_done()
            for job in controls:
                self._queue.put(job)
            return count

    def stop(self, stop_player: Callable[[], None]) -> None:
        """Serialize stop after any active upload, before subsequent audio."""
        with self._lock:
            self.clear()
            self._queue.put(
                AudioJob(
                    priority=Priority.CONTROL,
                    expires_at=float("inf"),
                    text="stop",
                    wav_paths=[],
                    stop_player=stop_player,
                )
            )

    def _worker(self) -> None:
        """Drain the queue, drop expired jobs, play ready jobs."""
        while True:
            job = self._queue.get()
            try:
                if job.stop_player is not None:
                    job.stop_player()
                    continue
                if job.generation != self._generation:
                    continue
                if time.monotonic() > job.expires_at:
                    logger.info("Race Voice dropped expired job: '%s'", job.text)
                    continue
                logger.info(
                    "Race Voice playing [%s]: %s",
                    job.priority.name,
                    ", ".join(p.name for p in job.wav_paths),
                )
                self._player(
                    job.text,
                    job.wav_paths,
                    job.priority,
                    job.expires_at,
                    job.play_at,
                    job.volume,
                )
            except Exception:
                logger.exception("Race Voice worker error for '%s'", job.text)
            finally:
                self._queue.task_done()
