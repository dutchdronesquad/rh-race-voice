"""Service-side phrase planning using the same localized segments as the plugin."""

from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path
from typing import TYPE_CHECKING

from custom_plugins.race_voice.services.clock_callouts import ClockCallouts
from custom_plugins.race_voice.services.lap_callouts import (
    CalloutSegment,
    LapCalloutSegments,
)

from .race_protocol import EventKind, RaceEvent

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Callable

    from .synthesis import SynthesisWorker

_LOCALES = json.loads(
    (
        Path(__file__).resolve().parents[1] / "custom_plugins/race_voice/locales.json"
    ).read_text(encoding="utf-8")
)


def _locale(model: str) -> dict:
    return _LOCALES.get(model[:2], _LOCALES["en"])


class SpeechEngine:
    """Plan one complete callout or an explicit, incremental pre-cache operation."""

    def __init__(self, worker: SynthesisWorker) -> None:
        """Keep construction free of model loading or automatic cache preparation."""
        self.worker = worker
        self._laps = LapCalloutSegments(locale_for_model=_locale)
        self._clock = ClockCallouts(locale_for_model=_locale)

    def segments(self, event: RaceEvent, model: str) -> tuple[CalloutSegment, ...]:
        """Preserve RH phonetic time and name, with service-localized lap numbers."""
        if event.kind == EventKind.TONE:
            return ()
        if event.kind == EventKind.LAP:
            return self._laps.plan(
                {"lap": event.lap, "pilot": event.pilot_name, "phonetic": event.text},
                model,
            ).segments
        subdir = "precache/clock" if event.kind == EventKind.COUNTDOWN else ""
        return (CalloutSegment(event.text or "", subdir),)

    async def synthesize(
        self,
        event: RaceEvent,
        settings: dict,
        *,
        deadline: float,
        priority: int,
        is_current: Callable[[], bool],
    ) -> tuple[bytes, ...]:
        """Do not return partial/obsolete speech after expiry, stop or heat changes."""
        audio = []
        for segment in self.segments(event, settings["model"]):
            if not is_current() or time.monotonic() >= deadline:
                return ()
            result = await self.worker.request(
                self._request(settings, segment),
                deadline=deadline,
                priority=priority,
            )
            if not is_current() or time.monotonic() >= deadline:
                return ()
            audio.append(result["audio"])
        return tuple(audio)

    async def prepare(
        self,
        settings: dict,
        pilot_names: list[str],
        *,
        is_current: Callable[[], bool],
    ) -> AsyncIterator[dict]:
        """Manually fill missing phrases, yielding progress between jobs."""
        if len(pilot_names) > 256:
            raise ValueError("Too many pilots to prepare")
        model = settings["model"]
        phrases = [
            CalloutSegment(p.text, p.subdir)
            for p in self._clock.precache_phrases(model)
        ]
        phrases.extend(
            CalloutSegment(text, "precache/clock")
            for text in _locale(model)["race_schedule"].values()
        )
        phrases.extend(self._laps.precache_segments(pilot_names, model))
        generated = reused = 0
        for index, phrase in enumerate(phrases, start=1):
            if not is_current():
                return
            result = await self.worker.request(
                self._request(settings, phrase),
                deadline=time.monotonic() + 120,
                priority=10,
            )
            if not is_current():
                return
            reused += int(result["cache_hit"])
            generated += int(not result["cache_hit"])
            yield {
                "completed": index,
                "total": len(phrases),
                "generated": generated,
                "reused": reused,
            }
            await asyncio.sleep(0)

    @staticmethod
    def _request(settings: dict, segment: CalloutSegment) -> dict:
        return {
            "operation": "synthesize",
            "model": settings["model"],
            "speed": settings["speed"],
            "noise": settings["noise"],
            "noise_w": settings["noise_w"],
            "text": segment.text,
            "subdir": segment.subdir,
        }
