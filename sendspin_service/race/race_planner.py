"""Bounded race-aware preparation and per-output playback planning.

These planners are owned by one asyncio loop. The source prepares immutable
audio once; each destination/selection applies the same playback policy.
"""

from __future__ import annotations

import asyncio
import contextlib
import io
import logging
import threading
import time
import wave
from dataclasses import dataclass, field, replace
from typing import TYPE_CHECKING, Protocol

from sendspin_service.playback.audio_queue import Priority, WavItem

from . import telemetry
from .race_protocol import EventKind, RaceEvent

if TYPE_CHECKING:
    from collections.abc import Callable

    from sendspin_service.playback.sendspin import SendSpinServer

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


def _supersede(
    pending: list[_Playback], predicate: Callable[[_Playback], bool]
) -> tuple[list[_Playback], list[_Playback]]:
    """Split pending into (kept, superseded) by predicate."""
    superseded = [p for p in pending if predicate(p)]
    kept = [p for p in pending if not predicate(p)]
    return kept, superseded


@dataclass(frozen=True)
class CalloutPlan:
    """A captured race event whose times are already mapped into this host's clock."""

    event: RaceEvent
    deadline: float
    target: float | None = None
    volume: float = 1.0
    audio: tuple[bytes, ...] = ()
    order: int = field(kw_only=True)
    completion: asyncio.Future[tuple[bytes, ...]] | None = field(
        default=None, kw_only=True, compare=False, repr=False
    )

    @property
    def retained_audio(self) -> tuple[bytes, ...]:
        """Include a completed continuation in queue byte and duration bounds."""
        if self.completion is not None and self.completion.done():
            return self.completion.result() or self.audio
        return self.audio

    async def complete_audio(self) -> tuple[bytes, ...]:
        """Await shared synthesis without letting one output cancel other outputs."""
        if self.completion is None:
            return self.audio
        async with asyncio.timeout_at(self.deadline):
            return await asyncio.shield(self.completion)

    @property
    def priority(self) -> Priority:
        """Use the same semantic priority at preparation and every output."""
        return priority_for(self.event)

    @property
    def duration_s(self) -> float:
        """Include newly completed speech when bounding audible backlog."""
        duration = 0.0
        for data in self.retained_audio:
            try:
                with wave.open(io.BytesIO(data), "rb") as wav:
                    duration += wav.getnframes() / wav.getframerate()
            except (EOFError, wave.Error):
                # Playback already skips unreadable WAVs. Count/byte bounds
                # still apply when a clip's duration cannot be determined.
                continue
        return duration


@dataclass
class _Preparation:
    plan: CalloutPlan
    settings: dict
    cancelled: bool = False
    published: bool = False
    enqueued_at: float = field(default_factory=time.monotonic)


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
        active = self._active
        supersedes_active = (
            active is not None
            and plan.priority == active.plan.priority == Priority.LAP
            and plan.event.pilot_id is not None
            and plan.event.pilot_id == active.plan.event.pilot_id
            and not active.published
        )
        if (
            active
            and not active.cancelled
            and (_preempts(plan.priority, active.plan.priority) or supersedes_active)
        ):
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
                    if not item.published:
                        telemetry.record(
                            item.plan.event.event_id,
                            "output_dropped",
                            reason="synthesis_failed",
                        )
                    logger.exception(
                        "Race Voice preparation failed: %s", item.plan.event.event_id
                    )
                finally:
                    self._active = None
                    self._inference = None

    async def _prepare(self, item: _Preparation) -> None:
        plan = item.plan
        completion = None

        def publish_prefix(audio: tuple[bytes, ...]) -> None:
            nonlocal completion
            if item.cancelled or not self._usable(plan):
                return
            completion = asyncio.get_running_loop().create_future()
            item.published = True
            self._ready(replace(plan, audio=audio, completion=completion))
            telemetry.record(plan.event.event_id, "prefix_ready")

        telemetry.record(
            plan.event.event_id,
            "synthesis_start",
            wait_ms=round((time.monotonic() - item.enqueued_at) * 1000, 1),
        )
        audio = ()
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
                    memory_cache_hit=result.get("memory_cache_hit", False),
                    duration_ms=result.get("duration_ms"),
                ),
                on_prefix=publish_prefix,
            )
        except asyncio.CancelledError:
            # A preempting signal cancels this task mid-await; without this the
            # event would keep no terminal telemetry record at all.
            if not item.published:
                telemetry.record(
                    plan.event.event_id, "output_dropped", reason="cancelled"
                )
            raise
        finally:
            if completion is not None and not completion.done():
                completion.set_result(
                    audio if not item.cancelled and self._usable(plan) else ()
                )
        telemetry.record(plan.event.event_id, "synthesis_end", empty=not audio)
        if completion is not None:
            # The reserved output slot owns delivery and its terminal telemetry.
            return
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


def accept_all(_plan: CalloutPlan) -> bool:
    """Default filter: a destination with no restriction receives every callout."""
    return True


@dataclass(frozen=True)
class Destination:
    """One named playback output and an optional filter over what it receives.

    Unmatched callouts are never submitted to this destination at all --
    they are not a bounded-queue drop, so they get no output_dropped record.
    """

    sink: PlaybackSink
    accepts: Callable[[CalloutPlan], bool] = accept_all


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
        queued = await self._play_audio(plan, cancelled, deadline)
        if queued:
            telemetry.record(
                plan.event.event_id, "output_played", destination=self._destination
            )
        else:
            # The backend can finish without delivery (no listener before the
            # end, expired, cancelled, or a stream error) without raising.
            telemetry.record(
                plan.event.event_id,
                "output_dropped",
                reason="sink",
                destination=self._destination,
            )

    async def _play_audio(
        self, plan: CalloutPlan, cancelled: threading.Event, deadline: float
    ) -> bool:
        complete = plan.completion is None or plan.completion.done()
        audio = await plan.complete_audio() if complete else plan.audio
        offset = 0
        while audio:
            if cancelled.is_set() or time.monotonic() >= deadline:
                return False
            clips = [
                WavItem(name=f"{plan.event.event_id}-{offset + index}.wav", data=data)
                for index, data in enumerate(audio)
            ]
            queued = await asyncio.to_thread(
                self._backend.play,
                clips,
                deadline,
                plan.target,
                plan.volume,
                cancelled=cancelled,
            )
            if not queued or complete:
                return bool(queued)
            # Keep this output's one callout slot until its time is appended.
            # The backend appends to the existing timeline, preserving the
            # prefix's client lead and preventing another pilot interleaving.
            offset += len(audio)
            try:
                full_audio = await plan.complete_audio()
            except TimeoutError:
                return False
            audio = full_audio[offset:]
            complete = True
        return False

    async def stop(self) -> None:
        """Wait for the actual stream clear before the next plan can play."""
        await asyncio.to_thread(self._backend.stop, strict=True)


@dataclass
class _Playback:
    plan: CalloutPlan
    cancelled: threading.Event = field(default_factory=threading.Event)
    enqueued_at: float = field(default_factory=time.monotonic)


class PlaybackPlanner:
    """Apply priority and cancellation independently for one destination/selection."""

    def __init__(  # noqa: PLR0913
        self,
        sink: PlaybackSink,
        *,
        is_current: Callable[[RaceEvent], bool],
        destination: str,
        max_pending: int = 32,
        max_bytes: int = 8 * 1024 * 1024,
        max_laps: int = 4,
        max_lap_audio_s: float = 4.0,
    ) -> None:
        """Bound both queue count and retained audio, independent of other outputs."""
        if (
            max_pending < 1
            or max_bytes < 1
            or max_laps < 1
            or not (0 < max_lap_audio_s < float("inf"))
        ):
            raise ValueError("Playback limits must be positive")
        self._sink = sink
        self._is_current = is_current
        self._destination = destination
        self._max_pending = max_pending
        self._max_bytes = max_bytes
        self._max_laps = max_laps
        self._max_lap_audio_s = max_lap_audio_s
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
        replays_active_tone = (
            plan.event.kind == EventKind.TONE
            and plan.priority == Priority.HIGH
            and self._active is not None
            and self._active.plan.event.kind == EventKind.TONE
            and self._active.plan.event.asset == plan.event.asset
        )
        if _preempts(plan.priority, self._last_priority) or replays_active_tone:
            self._interrupt()
        self._pending.append(_Playback(plan))
        if self._runner is None:
            self._runner = asyncio.create_task(self._run(), name="race-voice-playback")
        self._wake.set()
        return True

    def _admit_audio(self, plan: CalloutPlan) -> list[_Playback] | None:
        pending = list(self._pending)
        if plan.priority == Priority.SIGNAL:
            pending, superseded = _supersede(
                pending, lambda p: p.plan.priority == Priority.LAP
            )
            self._record_superseded(superseded)
        elif plan.priority == Priority.LAP and plan.event.pilot_id is not None:
            pending, superseded = _supersede(
                pending,
                lambda p: (
                    p.plan.priority == Priority.LAP
                    and p.plan.event.pilot_id == plan.event.pilot_id
                ),
            )
            self._record_superseded(superseded)
        if plan.event.kind == EventKind.TONE and plan.priority == Priority.HIGH:
            # Only the manual test tone (audio-check) is HIGH; staging tones
            # and the buzzer are SIGNAL and legitimately repeat the same
            # asset for distinct countdown steps, so this must not catch
            # them. Re-pressing the test button replaces any still-queued
            # copy of itself rather than piling up behind it, since
            # consecutive plays append to the same stream.
            pending, superseded = _supersede(
                pending,
                lambda p: (
                    p.plan.event.kind == EventKind.TONE
                    and p.plan.event.asset == plan.event.asset
                ),
            )
            self._record_superseded(superseded)
        backlog = (
            self._bound_lap_backlog(plan, pending)
            if plan.priority == Priority.LAP
            else []
        )
        # Reserve some memory for a signal while ordinary speech is still buffered.
        limit = self._max_bytes
        if plan.priority != Priority.SIGNAL:
            limit -= min(262_144, limit // 4)
        active_size = (
            sum(len(data) for data in self._active.plan.retained_audio)
            if self._active
            else 0
        )
        while True:
            size = active_size + sum(len(data) for data in plan.retained_audio)
            size += sum(len(data) for p in pending for data in p.plan.retained_audio)
            if len(pending) < self._max_pending and size <= limit:
                for dropped in backlog:
                    telemetry.record(
                        dropped.plan.event.event_id,
                        "output_dropped",
                        reason="lap_backlog",
                        destination=self._destination,
                    )
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
                if plan.event.kind == EventKind.TONE and not pending:
                    # A bundled tone (e.g. the audio-check track) can alone
                    # exceed the budget; once nothing else is queued to evict,
                    # admit it solo rather than leave it permanently unplayable.
                    return pending
                return None
            evicted = min(replaceable, key=lambda p: (-p.plan.priority, p.plan.order))
            pending.remove(evicted)
            telemetry.record(
                evicted.plan.event.event_id,
                "output_dropped",
                reason="evicted",
                destination=self._destination,
            )

    def _bound_lap_backlog(
        self, plan: CalloutPlan, pending: list[_Playback]
    ) -> list[_Playback]:
        """Keep recent laps without retaining many seconds of prepared old speech."""
        laps = sorted(
            (p for p in pending if p.plan.priority == Priority.LAP),
            key=lambda p: p.plan.order,
        )
        duration_s = plan.duration_s + sum(p.plan.duration_s for p in laps)
        dropped = []
        while laps and (
            len(laps) >= self._max_laps or duration_s > self._max_lap_audio_s
        ):
            oldest = laps.pop(0)
            duration_s -= oldest.plan.duration_s
            pending.remove(oldest)
            dropped.append(oldest)
        return dropped

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
                telemetry.record(
                    item.plan.event.event_id,
                    "playback_start",
                    wait_ms=round((time.monotonic() - item.enqueued_at) * 1000, 1),
                    duration_ms=round(item.plan.duration_s * 1000, 1),
                    destination=self._destination,
                )
                try:
                    await self._sink.play(item.plan, item.cancelled)
                except Exception:
                    self._interrupt()
                    logger.exception(
                        "Race Voice playback failed: %s", item.plan.event.event_id
                    )
                finally:
                    self._active = None
