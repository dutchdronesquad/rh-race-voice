"""Race clock callout phrase planning for Race Voice."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator, Sequence


DEFAULT_THRESHOLDS = (60, 30, 10)
PRECACHE_DIR_NAME = "clock"
PRECACHE_SUBDIR = f"precache/{PRECACHE_DIR_NAME}"


@dataclass(frozen=True)
class ClockCalloutPhrase:
    """One pre-cacheable race clock callout phrase."""

    text: str
    subdir: str = PRECACHE_SUBDIR


@dataclass(frozen=True)
class ClockCalloutPlan:
    """Playback plan for one race clock callout event."""

    kind: str
    seconds: int


class ClockCallouts:
    """Build race clock callout phrases for live playback and pre-cache."""

    def __init__(
        self,
        *,
        locale_for_model: Callable[[str], dict],
        thresholds: Sequence[int] = DEFAULT_THRESHOLDS,
    ) -> None:
        """Initialize phrase generation helpers."""
        self._locale_for_model = locale_for_model
        self._thresholds = tuple(thresholds)

    @property
    def subdir(self) -> str:
        """Return the cache subdirectory for race clock callout phrases."""
        return PRECACHE_SUBDIR

    @property
    def precache_dir_name(self) -> str:
        """Return the race clock callout directory under the model pre-cache root."""
        return PRECACHE_DIR_NAME

    def plan(self, seconds: object) -> ClockCalloutPlan | None:
        """Return the playback plan for a race clock callout event."""
        try:
            seconds = int(seconds)
        except (TypeError, ValueError):
            return None
        if seconds in self._thresholds:
            return ClockCalloutPlan("voice", seconds)
        if 1 <= seconds <= 5:
            return ClockCalloutPlan("tone", seconds)
        if seconds == 0:
            return ClockCalloutPlan("buzzer", seconds)
        return None

    def phrase(self, seconds: int | str, model_name: str) -> str:
        """Return the localized phrase for a race clock callout threshold."""
        locale = self._locale_for_model(model_name)
        return locale.get("clock_callout", {}).get(str(seconds), f"{seconds} seconds")

    def precache_phrases(self, model_name: str) -> Iterator[ClockCalloutPhrase]:
        """Yield all race clock callout phrases for manual pre-cache rebuilds."""
        for seconds in self._thresholds:
            yield ClockCalloutPhrase(self.phrase(seconds, model_name))
