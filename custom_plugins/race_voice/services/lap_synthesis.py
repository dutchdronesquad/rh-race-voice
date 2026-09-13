"""Bound pending lap synthesis while letting other executor work run between laps."""

from __future__ import annotations

import logging
import threading
import time
from collections import deque
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Callable
    from concurrent.futures import ThreadPoolExecutor

logger = logging.getLogger(__name__)
DEFAULT_PENDING_LAPS = 4


class LapSynthesisQueue:
    """Keep at most four fresh pending laps and a single active synthesis job."""

    def __init__(
        self,
        executor: ThreadPoolExecutor,
        synthesize: Callable[[dict[str, Any]], None],
        max_pending: int = DEFAULT_PENDING_LAPS,
    ) -> None:
        """Use the shared synthesis executor without filling its internal queue."""
        if max_pending < 1:
            raise ValueError("max_pending must be positive")
        self._executor = executor
        self._synthesize = synthesize
        self._max_pending = max_pending
        self._pending: deque[dict[str, Any]] = deque()
        self._lock = threading.Lock()
        self._active = False
        self._dropped = 0
        self._last_report = 0.0

    def submit(self, snapshot: dict[str, Any]) -> None:
        """Prefer the newest pending lap per pilot and evict oldest on overload."""
        with self._lock:
            now = time.monotonic()
            pilot = snapshot.get("pilot")
            retained = deque(
                job
                for job in self._pending
                if job["expires_at"] > now
                and (pilot is None or job.get("pilot") != pilot)
            )
            self._dropped += len(self._pending) - len(retained)
            self._pending = retained
            if snapshot["expires_at"] <= now:
                self._dropped += 1
            else:
                if len(self._pending) >= self._max_pending:
                    self._pending.popleft()
                    self._dropped += 1
                self._pending.append(snapshot)
            if self._dropped and now - self._last_report >= 5.0:
                logger.info("Race Voice skipped %d pending lap callouts", self._dropped)
                self._dropped = 0
                self._last_report = now
            if self._pending and not self._active:
                self._active = True
                self._executor.submit(self._run_one)

    def _run_one(self) -> None:
        """Yield the executor to other announcements after each complete lap."""
        try:
            with self._lock:
                snapshot = self._pending.popleft()
            if snapshot["expires_at"] > time.monotonic():
                self._synthesize(snapshot)
        except Exception:
            logger.exception("Race Voice lap synthesis failed")
        finally:
            with self._lock:
                if self._pending:
                    self._executor.submit(self._run_one)
                else:
                    self._active = False
