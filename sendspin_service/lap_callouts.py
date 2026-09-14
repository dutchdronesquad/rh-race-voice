"""Lap callout segment planning for Race Voice."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Callable, Iterable, Iterator


PILOT_SEGMENTS_SUBDIR = "precache/pilots"
LAP_SEGMENTS_SUBDIR = "precache/laps"
TMP_SEGMENTS_SUBDIR = "tmp"
DEFAULT_MAX_PRECACHED_LAPS = 6


@dataclass(frozen=True)
class CalloutSegment:
    """One synthesizeable WAV segment in a lap callout."""

    text: str
    subdir: str


@dataclass(frozen=True)
class LapCalloutPlan:
    """Segments and log label for one lap callout."""

    label: str
    segments: tuple[CalloutSegment, ...]


class LapCalloutSegments:
    """Build reusable lap-callout segments for live playback and pre-cache."""

    def __init__(
        self,
        *,
        locale_for_model: Callable[[str], dict],
        max_precached_laps: int = DEFAULT_MAX_PRECACHED_LAPS,
    ) -> None:
        """Initialize phrase generation helpers."""
        self._locale_for_model = locale_for_model
        self._max_precached_laps = max_precached_laps

    @property
    def precache_dir_names(self) -> tuple[str, ...]:
        """Return directory names relative to the model pre-cache directory."""
        return ("pilots", "laps")

    def plan(self, snapshot: dict[str, Any], model_name: str) -> LapCalloutPlan:
        """Build the ordered segment list for a live lap callout."""
        lap_number = snapshot["lap"]
        pilot_name = self._pilot_name(snapshot)
        phonetic_time = snapshot.get("phonetic")
        lap_phrase = self._lap_phrase(lap_number, model_name)

        segments: list[CalloutSegment] = []
        label_parts: list[str] = []

        if pilot_name:
            label_parts.append(pilot_name)
            segments.append(
                CalloutSegment(self._pilot_phrase(pilot_name), PILOT_SEGMENTS_SUBDIR)
            )

        label_parts.append(lap_phrase)
        segments.append(CalloutSegment(lap_phrase, LAP_SEGMENTS_SUBDIR))

        if phonetic_time:
            time_phrase = str(phonetic_time)
            label_parts.append(time_phrase)
            segments.append(CalloutSegment(time_phrase, TMP_SEGMENTS_SUBDIR))

        return LapCalloutPlan(label=", ".join(label_parts), segments=tuple(segments))

    def precache_segments(
        self, pilot_names: Iterable[str], model_name: str
    ) -> Iterator[CalloutSegment]:
        """Yield all reusable lap-callout segments for pre-cache."""
        yield from self.precache_lap_segments(model_name)
        yield from self.precache_pilot_segments(pilot_names)

    def precache_lap_segments(self, model_name: str) -> Iterator[CalloutSegment]:
        """Yield reusable lap-number segments for pre-cache."""
        for lap in range(1, self._max_precached_laps + 1):
            yield CalloutSegment(self._lap_phrase(lap, model_name), LAP_SEGMENTS_SUBDIR)

    def precache_pilot_segments(
        self, pilot_names: Iterable[str]
    ) -> Iterator[CalloutSegment]:
        """Yield reusable pilot-name segments for pre-cache."""
        seen_pilots: set[str] = set()
        for pilot_name in pilot_names:
            name = str(pilot_name).strip()
            name_key = name.casefold()
            if not name or name_key in seen_pilots:
                continue
            seen_pilots.add(name_key)
            yield CalloutSegment(self._pilot_phrase(name), PILOT_SEGMENTS_SUBDIR)

    @staticmethod
    def _pilot_name(snapshot: dict[str, Any]) -> str:
        name = snapshot.get("pilot") or snapshot.get("callsign") or ""
        return str(name).strip()

    @staticmethod
    def _pilot_phrase(pilot_name: str) -> str:
        return f"{pilot_name},"

    def _lap_phrase(self, lap_number: int, model_name: str) -> str:
        lap_word = self._locale_for_model(model_name).get("lap", "Lap")
        return f"{lap_word} {lap_number}"
