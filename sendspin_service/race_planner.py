"""Bounded race-aware preparation and per-output playback planning.

These planners are owned by one asyncio loop. The source prepares immutable
audio once; each destination/selection applies the same playback policy.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import threading
import time
from dataclasses import dataclass, field, replace
from typing import TYPE_CHECKING, Protocol

from . import telemetry
from .audio_queue import Priority, WavItem
from .race_protocol import EventKind, RaceEvent

if TYPE_CHECKING:
    from collections.abc import Callable

    from .sendspin import SendSpinServer
    from .speech import SpeechEngine

logger = logging.getLogger(__name__)


def priority_for(event: RaceEvent) -> Priority:
    """Preserve countdown-first and lap-last race behaviour."""
    if event.kind == EventKind.TONE and event.asset == "audio_check":
        return Priority.HIGH
    if event.kind in (EventKind.TONE, EventKind.COUNTDOWN):
        return Priority.SIGNAL
    if event.kind == EventKind.LAP:
        return Priority.LAP
    return Priority.HIGH if event.winner else Priority.NORMAL


def _preempts(new: Priority, old: Priority | None) -> bool:
    return (old == Priority.LAP and new < Priority.LAP) or (
        new == Priority.SIGNAL and old not in (None, Priority.SIGNAL)
    )


@dataclass(frozen=True)
class CalloutPlan:
    """A captured race event whose times are already mapped into this host's clock."""

    event: RaceEvent
    deadline: float
    target: float | None = None
    volume: float = 1.0
    audio: tuple[bytes, ...] = ()
    order: int = field(kw_only=True)

    @property
    def priority(self) -> Priority:
        """Use the same semantic priority at preparation and every output."""
        return priority_for(self.event)


@dataclass
class _Preparation:
    plan: CalloutPlan
    settings: dict
    cancelled: bool = False


class PreparationPlanner:
    """Bound speech work while delivering ready tones without using the worker."""

    def __init__(  # noqa: PLR0913
        self,
        speech: SpeechEngine,
        *,
        assets: dict[str, bytes],
        is_current: Callable[[RaceEvent], bool],
        ready: Callable[[CalloutPlan], None],
        max_laps: int = 4,
        max_announcements: int = 32,
    ) -> None:
        """Accept preloaded assets so even file I/O is outside the signal path."""
        if max_laps < 1 or max_announcements < 1:
            raise ValueError("Preparation limits must be positive")
        self._speech = speech
        self._assets = dict(assets)
        self._is_current = is_current
        self._ready = ready
        self._max_laps = max_laps
        self._max_announcements = max_announcements
        self._pending: list[_Preparation] = []
        self._active: _Preparation | None = None
        self._inference: asyncio.Task | None = None
        self._runner: asyncio.Task | None = None
        self._wake = asyncio.Event()
        self._closed = False

    def submit(self, plan: CalloutPlan, settings: dict) -> bool:
        """Admit fresh work without waiting for synthesis, audio or networking."""
        if not self._usable(plan):
            telemetry.record(plan.event.event_id, "output_dropped", reason="expired")
            return False
        if plan.event.kind == EventKind.TONE and plan.event.asset not in self._assets:
            telemetry.record(
                plan.event.event_id, "output_dropped", reason="missing_asset"
            )
            return False
        if plan.event.kind != EventKind.TONE and not self._make_room(plan):
            telemetry.record(plan.event.event_id, "output_dropped", reason="no_room")
            return False
        if plan.priority == Priority.SIGNAL:
            self._pending = [
                p for p in self._pending if p.plan.priority != Priority.LAP
            ]
        if self._active and _preempts(plan.priority, self._active.plan.priority):
            self._active.cancelled = True
            if self._inference is not None:
                self._inference.cancel()
        if plan.event.kind == EventKind.TONE:
            self._ready(replace(plan, audio=(self._assets[plan.event.asset],)))
            return True
        self._pending.append(_Preparation(plan, dict(settings)))
        if self._runner is None:
            self._runner = asyncio.create_task(
                self._run(), name="race-voice-preparation"
            )
        self._wake.set()
        return True

    def invalidate(self) -> None:
        """Call after installing new source state, before acknowledging a stop/reset."""
        self._pending = [p for p in self._pending if self._usable(p.plan)]
        if self._active and not self._usable(self._active.plan) and self._inference:
            self._active.cancelled = True
            self._inference.cancel()

    async def close(self) -> None:
        """Stop preparation without closing a separately owned shared worker."""
        self._closed = True
        self._pending.clear()
        if self._runner is not None:
            self._runner.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._runner
        self._runner = None

    def _usable(self, plan: CalloutPlan) -> bool:
        return (
            not self._closed
            and plan.deadline > time.monotonic()
            and self._is_current(plan.event)
        )

    def _make_room(self, plan: CalloutPlan) -> bool:
        self._pending = [p for p in self._pending if self._usable(p.plan)]
        is_lap = plan.priority == Priority.LAP
        same_class = [
            p for p in self._pending if (p.plan.priority == Priority.LAP) == is_lap
        ]
        if not is_lap:
            if len(same_class) < self._max_announcements:
                return True
            lower = [p for p in same_class if p.plan.priority > plan.priority]
            if not lower:
                return False
            evicted = min(lower, key=lambda p: (-p.plan.priority, p.plan.order))
            self._pending.remove(evicted)
            telemetry.record(
                evicted.plan.event.event_id, "output_dropped", reason="evicted"
            )
            return True
        replacement = next(
            (
                p
                for p in same_class
                if plan.event.pilot_id is not None
                and p.plan.event.pilot_id == plan.event.pilot_id
            ),
            None,
        )
        if replacement is None and len(same_class) >= self._max_laps:
            replacement = min(same_class, key=lambda p: p.plan.order)
        if replacement is not None:
            self._pending.remove(replacement)
            telemetry.record(
                replacement.plan.event.event_id, "output_dropped", reason="evicted"
            )
        return True

    async def _run(self) -> None:
        while not self._closed:
            await self._wake.wait()
            self._wake.clear()
            while self._pending:
                item = min(
                    self._pending,
                    key=lambda p: (p.plan.priority, p.plan.order),
                )
                self._pending.remove(item)
                if not self._usable(item.plan):
                    continue
                self._active = item
                self._inference = asyncio.create_task(self._prepare(item))
                try:
                    await self._inference
                except asyncio.CancelledError:
                    if self._closed:
                        raise
                except Exception:
                    logger.exception(
                        "Race Voice preparation failed: %s", item.plan.event.event_id
                    )
                finally:
                    self._active = None
                    self._inference = None

    async def _prepare(self, item: _Preparation) -> None:
        plan = item.plan
        telemetry.record(plan.event.event_id, "synthesis_start")
        try:
            audio = await self._speech.synthesize(
                plan.event,
                item.settings,
                deadline=plan.deadline,
                priority=plan.priority,
                is_current=lambda: not item.cancelled and self._usable(plan),
                on_segment=lambda result: telemetry.record(
                    plan.event.event_id,
                    "segment",
                    cache_hit=result.get("cache_hit"),
                    duration_ms=result.get("duration_ms"),
                ),
            )
        except asyncio.CancelledError:
            # A preempting signal cancels this task mid-await; without this the
            # event would keep no terminal telemetry record at all.
            telemetry.record(plan.event.event_id, "output_dropped", reason="cancelled")
            raise
        telemetry.record(plan.event.event_id, "synthesis_end", empty=not audio)
        if audio and not item.cancelled and self._usable(plan):
            self._ready(replace(plan, audio=audio))
        else:
            reason = (
                "synthesis_empty"
                if not audio
                else ("cancelled" if item.cancelled else "expired")
            )
            telemetry.record(plan.event.event_id, "output_dropped", reason=reason)


class PlaybackSink(Protocol):
    """One output stream; play must observe cancellation before committing chunks."""

    async def play(self, plan: CalloutPlan, cancelled: threading.Event) -> None:
        """Deliver audio without extending its deadline or scheduled target."""
        ...

    async def stop(self) -> None:
        """Clear buffered audio and return when subsequent playback is safe."""
        ...


class SendspinPlaybackSink:
    """Adapt one Sendspin backend to the planner without another semantic queue."""

    def __init__(self, backend: SendSpinServer, *, destination: str) -> None:
        """Use a backend whose lifecycle is managed by the owner."""
        self._backend = backend
        self._destination = destination

    async def play(self, plan: CalloutPlan, cancelled: threading.Event) -> None:
        """Retain the backend's final expiry check after client lead is calculated."""
        deadline = plan.deadline
        if plan.target is not None and plan.event.kind == EventKind.TONE:
            lateness = 1.0 if plan.event.asset == "buzzer" else 0.25
            deadline = min(deadline, plan.target + lateness)
        if cancelled.is_set() or deadline <= time.monotonic():
            telemetry.record(
                plan.event.event_id,
                "output_dropped",
                reason="cancelled" if cancelled.is_set() else "expired",
                destination=self._destination,
            )
            return
        clips = [
            WavItem(name=f"{plan.event.event_id}-{index}.wav", data=data)
            for index, data in enumerate(plan.audio)
        ]
        queued = await asyncio.to_thread(
            self._backend.play,
            clips,
            deadline,
            plan.target,
            plan.volume,
            cancelled=cancelled,
        )
        if queued:
            telemetry.record(
                plan.event.event_id, "output_played", destination=self._destination
            )
        else:
            # The backend can no-op (no connected clients, not ready, stream
            # error) without raising; don't count that as audible playback.
            telemetry.record(
                plan.event.event_id,
                "output_dropped",
                reason="sink",
                destination=self._destination,
            )

    async def stop(self) -> None:
        """Wait for the actual stream clear before the next plan can play."""
        await asyncio.to_thread(self._backend.stop, strict=True)


@dataclass
class _Playback:
    plan: CalloutPlan
    cancelled: threading.Event = field(default_factory=threading.Event)


class PlaybackPlanner:
    """Apply priority and cancellation independently for one destination/selection."""

    def __init__(
        self,
        sink: PlaybackSink,
        *,
        is_current: Callable[[RaceEvent], bool],
        destination: str,
        max_pending: int = 32,
        max_bytes: int = 8 * 1024 * 1024,
    ) -> None:
        """Bound both queue count and retained audio, independent of other outputs."""
        if max_pending < 1 or max_bytes < 1:
            raise ValueError("Playback limits must be positive")
        self._sink = sink
        self._is_current = is_current
        self._destination = destination
        self._max_pending = max_pending
        self._max_bytes = max_bytes
        self._pending: list[_Playback] = []
        self._active: _Playback | None = None
        self._last_priority: Priority | None = None
        self._stop_task: asyncio.Task | None = None
        self._runner: asyncio.Task | None = None
        self._wake = asyncio.Event()
        self._closed = False

    def submit(self, plan: CalloutPlan) -> bool:
        """Accept prepared audio; source synthesis is never repeated per listener."""
        if not plan.audio or not self._usable(plan):
            telemetry.record(
                plan.event.event_id,
                "output_dropped",
                reason="expired",
                destination=self._destination,
            )
            return False
        self._pending = [p for p in self._pending if self._usable(p.plan)]
        pending = self._admit_audio(plan)
        if pending is None:
            telemetry.record(
                plan.event.event_id,
                "output_dropped",
                reason="no_room",
                destination=self._destination,
            )
            return False
        self._pending = pending
        if _preempts(plan.priority, self._last_priority):
            self._interrupt()
        self._pending.append(_Playback(plan))
        if self._runner is None:
            self._runner = asyncio.create_task(self._run(), name="race-voice-playback")
        self._wake.set()
        return True

    def _admit_audio(self, plan: CalloutPlan) -> list[_Playback] | None:
        pending = list(self._pending)
        if plan.priority == Priority.SIGNAL:
            superseded = [p for p in pending if p.plan.priority == Priority.LAP]
            pending = [p for p in pending if p.plan.priority != Priority.LAP]
            self._record_superseded(superseded)
        elif plan.priority == Priority.LAP and plan.event.pilot_id is not None:
            superseded = [
                p
                for p in pending
                if p.plan.priority == Priority.LAP
                and p.plan.event.pilot_id == plan.event.pilot_id
            ]
            pending = [
                p
                for p in pending
                if not (
                    p.plan.priority == Priority.LAP
                    and p.plan.event.pilot_id == plan.event.pilot_id
                )
            ]
            self._record_superseded(superseded)
        # Reserve some memory for a signal while ordinary speech is still buffered.
        limit = self._max_bytes
        if plan.priority != Priority.SIGNAL:
            limit -= min(262_144, limit // 4)
        active_size = (
            sum(len(data) for data in self._active.plan.audio) if self._active else 0
        )
        while True:
            size = active_size + sum(len(data) for data in plan.audio)
            size += sum(len(data) for p in pending for data in p.plan.audio)
            if len(pending) < self._max_pending and size <= limit:
                return pending
            replaceable = [
                p
                for p in pending
                if (
                    p.plan.priority > plan.priority
                    or p.plan.priority == plan.priority == Priority.LAP
                )
            ]
            if not replaceable:
                return None
            evicted = min(replaceable, key=lambda p: (-p.plan.priority, p.plan.order))
            pending.remove(evicted)
            telemetry.record(
                evicted.plan.event.event_id,
                "output_dropped",
                reason="evicted",
                destination=self._destination,
            )

    def _record_superseded(self, superseded: list[_Playback]) -> None:
        for item in superseded:
            telemetry.record(
                item.plan.event.event_id,
                "output_dropped",
                reason="superseded",
                destination=self._destination,
            )

    def invalidate(self) -> None:
        """Discard previous source state and flush all currently buffered audio."""
        self._pending.clear()
        self._last_priority = None
        self._interrupt()

    async def flush(self) -> None:
        """Acknowledge a source stop only after the sink has cleared its buffer."""
        self.invalidate()
        if self._stop_task is not None:
            await asyncio.shield(self._stop_task)

    async def close(self) -> None:
        """Flush and finish this output without touching other planners."""
        self._closed = True
        self.invalidate()
        if self._runner is not None:
            self._runner.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._runner
        if self._stop_task is not None:
            await self._stop_task

    def _usable(self, plan: CalloutPlan) -> bool:
        return (
            not self._closed
            and plan.deadline > time.monotonic()
            and self._is_current(plan.event)
        )

    def _interrupt(self) -> None:
        if self._active is not None:
            self._active.cancelled.set()
        if self._stop_task is None or self._stop_task.done():
            self._stop_task = asyncio.create_task(self._sink.stop())

    async def _run(self) -> None:
        while not self._closed:
            await self._wake.wait()
            self._wake.clear()
            while self._pending:
                if self._stop_task is not None:
                    try:
                        await asyncio.shield(self._stop_task)
                    except Exception:
                        logger.exception("Race Voice output could not be stopped")
                        self._pending.clear()
                        break
                if not self._pending:
                    break
                item = min(
                    self._pending,
                    key=lambda p: (p.plan.priority, p.plan.order),
                )
                self._pending.remove(item)
                if not self._usable(item.plan):
                    continue
                self._active = item
                self._last_priority = item.plan.priority
                try:
                    await self._sink.play(item.plan, item.cancelled)
                except Exception:
                    self._interrupt()
                    logger.exception(
                        "Race Voice playback failed: %s", item.plan.event.event_id
                    )
                finally:
                    self._active = None
