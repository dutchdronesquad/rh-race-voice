"""Manual pre-cache rebuild orchestration for Race Voice."""

from __future__ import annotations

import contextlib
import logging
import threading
import time
from typing import TYPE_CHECKING, Any

from . import schedule

if TYPE_CHECKING:
    from collections.abc import Callable
    from concurrent.futures import Future, ThreadPoolExecutor
    from pathlib import Path

    from .clock_callouts import ClockCallouts
    from .lap_callouts import LapCalloutSegments

logger = logging.getLogger(__name__)


class PrecacheManager:
    """Own manual pre-cache rebuilds, stale-job cancellation, and reporting."""

    def __init__(  # noqa: PLR0913
        self,
        *,
        tts: Any,
        lap_callouts: LapCalloutSegments,
        synth_pool: ThreadPoolExecutor,
        prepare_model: Callable[[Any], None],
        clock_callouts: ClockCallouts,
        schedule_phrase: Callable[[int, str], str],
        pilot_names_for_heat: Callable[[int], list[str]],
        heat_name_for_id: Callable[[int], str | int],
        notify: Callable[[str], None],
    ) -> None:
        """Initialize rebuild dependencies."""
        self._tts = tts
        self._lap_callouts = lap_callouts
        self._synth_pool = synth_pool
        self._prepare_model = prepare_model
        self._clock_callouts = clock_callouts
        self._schedule_phrase = schedule_phrase
        self._pilot_names_for_heat = pilot_names_for_heat
        self._heat_name_for_id = heat_name_for_id
        self._notify = notify
        self._generation = 0
        self._lock = threading.Lock()

    def cancel(self) -> None:
        """Mark all in-flight pre-cache jobs stale."""
        self._next_generation()

    def rebuild(self, settings: Any, heat_id: int | None) -> None:
        """Clear and regenerate pre-cached phrases for current settings and heat."""
        generation = self._next_generation()
        self._clear_precache(settings.model_name)
        self._notify("Race Voice: rebuilding pre-cache...")

        future = self._synth_pool.submit(
            self._rebuild,
            settings,
            generation,
            heat_id,
        )
        future.add_done_callback(
            lambda f: self._on_rebuild_done(f, generation, heat_id)
        )

    def _next_generation(self) -> int:
        with self._lock:
            self._generation += 1
            return self._generation

    def _is_current(self, generation: int) -> bool:
        with self._lock:
            return generation == self._generation

    def _clear_precache(self, model_name: str) -> None:
        precache_dir = self._tts.precache_dir_for_model(model_name)
        for dir_name in self._lap_callouts.precache_dir_names:
            self._clear_wavs(precache_dir / dir_name, f"pre-cache {dir_name}")
        self._clear_wavs(
            precache_dir / self._clock_callouts.precache_dir_name,
            "pre-cache clock",
        )
        self._clear_wavs(
            precache_dir / schedule.PRECACHE_DIR_NAME,
            "pre-cache schedule",
        )

    def _rebuild(self, settings: Any, generation: int, heat_id: int | None) -> int:
        count = 0
        self._prepare_model(settings)
        count += self._precache_clock_callouts(settings, generation)
        count += self._precache_schedule(settings, generation)
        count += self._precache_laps(settings, generation)
        if heat_id and self._is_current(generation):
            count += self._precache_pilots(heat_id, generation, settings)
        return count

    def _precache_clock_callouts(self, settings: Any, generation: int) -> int:
        """Regenerate race clock callout phrases for the current params."""
        if not self._is_current(generation):
            return 0
        count = 0
        for phrase in self._clock_callouts.precache_phrases(settings.model_name):
            if not self._is_current(generation):
                logger.info("Race Voice stopped stale clock callout pre-cache job")
                return count
            count += self._precache_phrase(phrase.text, phrase.subdir, settings)
        return count

    def _precache_schedule(self, settings: Any, generation: int) -> int:
        """Regenerate schedule countdown phrases for the current params."""
        if not self._is_current(generation):
            return 0
        count = 0
        for threshold in schedule.DEFAULT_THRESHOLDS:
            if not self._is_current(generation):
                logger.info("Race Voice stopped stale schedule pre-cache job")
                return count
            count += self._precache_phrase(
                self._schedule_phrase(threshold, settings.model_name),
                schedule.PRECACHE_SUBDIR,
                settings,
            )
        return count

    def _precache_laps(self, settings: Any, generation: int) -> int:
        """Pre-synthesize heat-independent lap-number segments."""
        count = 0
        for segment in self._lap_callouts.precache_lap_segments(settings.model_name):
            if not self._is_current(generation):
                logger.info("Race Voice stopped stale lap pre-cache job")
                return count
            count += self._precache_phrase(segment.text, segment.subdir, settings)
        return count

    def _precache_pilots(self, heat_id: int, generation: int, settings: Any) -> int:
        """Pre-synthesize pilot-name segments for the current heat."""
        started = time.perf_counter()
        count = 0

        for segment in self._lap_callouts.precache_pilot_segments(
            self._pilot_names_for_heat(heat_id)
        ):
            if self._precache_stopped(generation, heat_id):
                return count
            count += self._precache_phrase(segment.text, segment.subdir, settings)
        elapsed_ms = int((time.perf_counter() - started) * 1000)
        logger.info(
            "Race Voice pre-cached %d new WAV(s) for heat %s in %dms",
            count,
            heat_id,
            elapsed_ms,
        )
        return count

    def _precache_phrase(self, text: str, subdir: str, settings: Any) -> int:
        result = self._tts.synthesize_to_cache(
            text=text,
            model_name=settings.model_name,
            params=settings.params,
            subdir=subdir,
        )
        return int(result is not None and not result.cache_hit)

    def _precache_stopped(self, generation: int, heat_id: int) -> bool:
        if self._is_current(generation):
            return False
        logger.info("Race Voice stopped stale pre-cache job for heat %s", heat_id)
        return True

    def _on_rebuild_done(
        self, future: Future, generation: int, heat_id: int | None
    ) -> None:
        if not self._is_current(generation):
            return
        try:
            count = future.result() or 0
        except Exception:
            logger.exception("Race Voice pre-cache rebuild failed")
            return

        with contextlib.suppress(Exception):
            if heat_id:
                heat_name = self._heat_name_for_id(heat_id)
                self._notify(
                    f"Race Voice: pre-cache rebuild complete for {heat_name}"
                    f" ({count} new WAV files)"
                )
            else:
                self._notify(
                    f"Race Voice: pre-cache rebuild complete ({count} new WAV files)"
                )

    @staticmethod
    def _clear_wavs(directory: Path, label: str) -> None:
        """Delete WAV files under a cache subdirectory."""
        if not directory.exists():
            return
        count = sum(
            1
            for wav_file in directory.rglob("*.wav")
            if wav_file.unlink(missing_ok=True) is None
        )
        if count:
            logger.info("Race Voice cleared %d %s WAV files", count, label)
