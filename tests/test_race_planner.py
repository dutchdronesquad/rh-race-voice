"""Check race freshness and priority independently of Piper or network timing."""

# ruff: noqa: PT009, SLF001

from __future__ import annotations

import asyncio
import json
import threading
import time
import unittest
from dataclasses import replace
from unittest.mock import AsyncMock, Mock

from sendspin_service.race_planner import (
    CalloutPlan,
    PlaybackPlanner,
    PreparationPlanner,
    SendspinPlaybackSink,
)
from sendspin_service.race_protocol import EventKind, RaceEvent
from tests.test_race_protocol import event_payload


def plan(sequence: int, kind: EventKind = EventKind.LAP, pilot: int = 1) -> CalloutPlan:
    """Use monotonic host deadlines, separate from source clock values."""
    event = replace(
        RaceEvent.parse(event_payload(max(1, sequence))),
        sequence=sequence,
        kind=kind,
        pilot_id=pilot,
        asset="stage" if kind == EventKind.TONE else None,
    )
    return CalloutPlan(event, time.monotonic() + 5, audio=(b"wav",), order=sequence)


async def until(predicate) -> None:  # noqa: ANN001
    """Bound failure time while allowing deterministic event-loop progress."""
    async with asyncio.timeout(1):
        while not predicate():  # noqa: ASYNC110
            await asyncio.sleep(0)


class PreparationPlannerTests(unittest.IsolatedAsyncioTestCase):
    """Admission must be fast and race signals must not depend on speech readiness."""

    async def asyncSetUp(self) -> None:
        """Control inference and capture complete callouts delivered to routing."""
        self.speech = Mock()
        self.speech.synthesize = AsyncMock(return_value=(b"speech",))
        self.ready = Mock()
        self.current = True
        self.planner = PreparationPlanner(
            self.speech,
            assets={"stage": b"beep"},
            is_current=lambda _: self.current,
            ready=self.ready,
        )
        self.addAsyncCleanup(self.planner.close)

    async def test_burst_bounds_laps_and_keeps_latest_per_pilot(self) -> None:
        """Do not admit unlimited inference work before the runner gets CPU."""
        for number in range(1, 100):
            self.planner.submit(plan(number, pilot=number % 5), {})
        self.assertEqual(len(self.planner._pending), 4)
        await until(lambda: self.ready.call_count == 4)
        self.assertEqual(
            [c.args[0].event.sequence for c in self.ready.call_args_list],
            [96, 97, 98, 99],
        )

    async def test_internal_countdown_keeps_service_order_through_preparation(
        self,
    ) -> None:
        """Sequence zero must not move internal speech ahead of an earlier signal."""
        self.planner.submit(replace(plan(20, EventKind.COUNTDOWN), order=1), {})
        self.planner.submit(
            replace(
                plan(0, EventKind.COUNTDOWN),
                order=2,
            ),
            {},
        )
        await until(lambda: self.ready.call_count == 2)
        self.assertEqual(
            [call.args[0].event.sequence for call in self.ready.call_args_list], [20, 0]
        )
        self.assertEqual(
            [call.args[0].order for call in self.ready.call_args_list], [1, 2]
        )
        self.planner.submit(replace(plan(21, EventKind.TONE), order=3), {})
        self.assertEqual(self.ready.call_args.args[0].order, 3)

    async def test_beep_bypasses_active_speech_and_drops_obsolete_laps(self) -> None:
        """Ignoring task cancellation cannot revive interrupted audio."""
        entered = asyncio.Event()

        async def stubborn(*_args, **_kwargs):  # noqa: ANN002, ANN003, ANN202
            entered.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                return (b"obsolete",)

        self.speech.synthesize.side_effect = stubborn
        self.planner.submit(plan(1), {})
        await entered.wait()
        self.planner.submit(plan(2), {})
        self.planner.submit(plan(3, EventKind.TONE), {})
        self.assertEqual(self.ready.call_count, 1)
        self.assertEqual(self.ready.call_args.args[0].audio, (b"beep",))
        await until(lambda: self.planner._active is None)
        self.assertEqual(self.ready.call_count, 1)

    async def test_state_change_discards_late_result(self) -> None:
        """Context invalidation applies both before and after await boundaries."""
        release = asyncio.Event()

        async def generate(*_args, **_kwargs):  # noqa: ANN002, ANN003, ANN202
            await release.wait()
            return (b"old",)

        self.speech.synthesize.side_effect = generate
        self.planner.submit(plan(1), {})
        await until(lambda: self.planner._active is not None)
        self.current = False
        self.planner.invalidate()
        release.set()
        await until(lambda: self.planner._active is None)
        self.ready.assert_not_called()

    async def test_cache_flush_cannot_revive_cancelled_inference(self) -> None:
        """Restoring admission after a flush cannot revive a swallowed cancellation."""
        entered = asyncio.Event()

        async def stubborn(*_args, **_kwargs):  # noqa: ANN002, ANN003, ANN202
            entered.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                return (b"obsolete",)

        self.speech.synthesize.side_effect = stubborn
        self.planner.submit(plan(1), {})
        await entered.wait()
        self.current = False
        self.planner.invalidate()
        self.current = True
        await until(lambda: self.planner._active is None)
        self.ready.assert_not_called()

    async def test_preempted_inference_still_records_a_terminal_drop(self) -> None:
        """Cancellation mid-await must not leave an event with no terminal record."""
        entered = asyncio.Event()

        async def blocked(*_args, **_kwargs):  # noqa: ANN002, ANN003, ANN202
            entered.set()
            await asyncio.Event().wait()

        self.speech.synthesize.side_effect = blocked
        with self.assertLogs("sendspin_service.telemetry", level="INFO") as captured:
            self.planner.submit(plan(1), {})
            await entered.wait()
            self.planner.submit(plan(2, EventKind.TONE), {})  # preempts the lap
            await until(lambda: self.planner._active is None)
        drops = [
            json.loads(entry.getMessage())
            for entry in captured.records
            if json.loads(entry.getMessage())["stage"] == "output_dropped"
        ]
        self.assertTrue(any(record["reason"] == "cancelled" for record in drops))

    async def test_rejected_or_expired_tone_cannot_interrupt(self) -> None:
        """A missing asset or missed deadline must not silence usable speech."""
        bad = plan(1, EventKind.TONE)
        bad = replace(bad, event=replace(bad.event, asset="missing"))
        self.assertFalse(self.planner.submit(bad, {}))
        self.assertFalse(
            self.planner.submit(replace(plan(2, EventKind.TONE), deadline=0), {})
        )
        self.ready.assert_not_called()

    async def test_announcement_limit_is_independent_of_laps(self) -> None:
        """A lap burst cannot consume all voice admission capacity."""
        for number in range(1, 8):
            self.planner.submit(plan(number, pilot=number), {})
        self.assertTrue(self.planner.submit(plan(9, EventKind.VOICE), {}))
        await until(lambda: self.ready.call_count == 5)
        self.assertEqual(self.ready.call_args_list[0].args[0].event.sequence, 9)


class _Sink:
    def __init__(self) -> None:
        self.played: list[CalloutPlan] = []
        self.cancelled: list[threading.Event] = []
        self.release = asyncio.Event()
        self.stop_release = asyncio.Event()
        self.stop_release.set()
        self.stops = 0

    async def play(self, callout: CalloutPlan, cancelled: threading.Event) -> None:
        self.played.append(callout)
        self.cancelled.append(cancelled)
        await self.release.wait()

    async def stop(self) -> None:
        self.stops += 1
        await self.stop_release.wait()
        self.release.set()


class PlaybackPlannerTests(unittest.IsolatedAsyncioTestCase):
    """Exercise independent output queues with playback and stop barriers."""

    async def asyncSetUp(self) -> None:
        """Block playback until explicitly released or interrupted."""
        self.sink = _Sink()
        self.current = True
        self.planner = PlaybackPlanner(
            self.sink, is_current=lambda _: self.current, destination="local"
        )
        self.addAsyncCleanup(self.planner.close)

    async def test_ready_signals_use_service_order_instead_of_source_sequence(
        self,
    ) -> None:
        """Ready stage tones remain ahead of later internal countdowns."""
        self.sink.release.set()
        self.planner.submit(replace(plan(20, EventKind.TONE), order=1))
        self.planner.submit(
            replace(
                plan(0, EventKind.COUNTDOWN),
                order=2,
            )
        )
        self.planner.submit(replace(plan(21, EventKind.TONE), order=3))
        await until(lambda: len(self.sink.played) == 3)
        self.assertEqual(
            [item.event.sequence for item in self.sink.played], [20, 0, 21]
        )

    async def test_countdown_cancels_lap_and_waits_for_stop(self) -> None:
        """Do not send the fresh signal until old buffered audio has been cleared."""
        self.planner.submit(plan(1))
        await until(lambda: len(self.sink.played) == 1)
        self.planner.submit(plan(2))
        self.sink.stop_release.clear()
        self.planner.submit(plan(3, EventKind.COUNTDOWN))
        self.assertTrue(self.sink.cancelled[0].is_set())
        await until(lambda: self.sink.stops == 1)
        self.assertEqual(len(self.sink.played), 1)
        self.sink.stop_release.set()
        await until(lambda: len(self.sink.played) == 2)
        self.assertEqual(self.sink.played[-1].event.sequence, 3)

    async def test_general_announcement_precedes_pending_laps(self) -> None:
        """All announcements have precedence while retaining their separate priority."""
        self.sink.release.set()
        self.planner.submit(plan(1))
        self.planner.submit(plan(2, EventKind.VOICE))
        await until(lambda: len(self.sink.played) == 2)
        self.assertEqual([p.event.sequence for p in self.sink.played], [2, 1])

    async def test_stop_invalidates_pending_and_active_audio(self) -> None:
        """A heat/reset fence must apply even while the sink is blocked."""
        self.planner.submit(plan(1))
        await until(lambda: len(self.sink.played) == 1)
        self.planner.submit(plan(2))
        self.current = False
        self.planner.invalidate()
        self.assertTrue(self.sink.cancelled[0].is_set())
        await until(lambda: self.planner._active is None)
        self.assertEqual(len(self.sink.played), 1)

    async def test_slow_cloud_queue_does_not_block_local_queue(self) -> None:
        """Each destination owns its waits and receives already-shared audio."""
        self.sink.release.set()  # self.planner ("local") proceeds immediately
        cloud_sink = _Sink()  # left blocked, standing in for a slow cloud relay
        cloud = PlaybackPlanner(
            cloud_sink, is_current=lambda _: True, destination="cloud"
        )
        self.addAsyncCleanup(cloud.close)
        first = plan(1)
        self.planner.submit(first)
        cloud.submit(first)
        self.planner.submit(plan(2, pilot=2))
        await until(lambda: len(self.sink.played) == 2)
        self.assertEqual(len(cloud_sink.played), 1)
        self.assertIs(cloud_sink.played[0].audio, self.sink.played[0].audio)

    async def test_expiry_and_memory_limits_prevent_unbounded_backlog(self) -> None:
        """Reject unusable jobs before any interrupt or extra retained audio."""
        self.planner._max_bytes = 5
        self.assertTrue(self.planner.submit(plan(1)))
        self.assertFalse(self.planner.submit(replace(plan(2), audio=(b"too large",))))
        self.assertFalse(
            self.planner.submit(replace(plan(3, EventKind.TONE), deadline=0))
        )
        self.assertEqual(self.sink.stops, 0)

    async def test_new_lap_replaces_buffered_old_lap_for_same_pilot(self) -> None:
        """A player's backlog should favour a fresh lap before committing audio."""
        self.sink.release.set()
        self.planner.submit(plan(1))
        self.planner.submit(plan(2))
        await until(lambda: len(self.sink.played) == 1)
        self.assertEqual(self.sink.played[0].event.sequence, 2)

    async def test_higher_priority_can_displace_full_pending_queue(self) -> None:
        """Capacity held by lower-priority speech must not exclude a race signal."""
        self.planner._max_pending = 1
        self.planner.submit(plan(1, EventKind.VOICE))
        self.assertTrue(self.planner.submit(plan(2, EventKind.TONE)))
        self.sink.release.set()
        await until(lambda: len(self.sink.played) == 1)
        self.assertEqual(self.sink.played[0].event.kind, EventKind.TONE)


class SendspinSinkTests(unittest.IsolatedAsyncioTestCase):
    """Keep the final client-buffer expiry check in the actual backend path."""

    async def test_scheduled_tone_passes_strict_latest_start_to_backend(self) -> None:
        """Client lead may not turn a missed countdown into an arbitrarily late beep."""
        backend = Mock()
        sink = SendspinPlaybackSink(backend, destination="local")
        target = time.monotonic() + 2
        callout = replace(plan(1, EventKind.TONE), target=target)
        cancelled = threading.Event()
        await sink.play(callout, cancelled)
        args = backend.play.call_args.args
        self.assertEqual(args[0][0].data, b"wav")
        self.assertEqual(args[1], target + 0.25)
        self.assertEqual(args[2], target)
        self.assertIs(backend.play.call_args.kwargs["cancelled"], cancelled)

    async def test_expired_tone_does_not_call_playback_backend(self) -> None:
        """Do not interrupt/start transport for audio whose latest start was missed."""
        backend = Mock()
        sink = SendspinPlaybackSink(backend, destination="local")
        await sink.play(replace(plan(1, EventKind.TONE), target=0), threading.Event())
        backend.play.assert_not_called()

    async def test_backend_no_op_is_recorded_as_dropped_not_played(self) -> None:
        """A silent backend no-op (e.g. no clients) must not count as played."""
        backend = Mock()
        backend.play.return_value = False
        sink = SendspinPlaybackSink(backend, destination="local")
        with self.assertLogs("sendspin_service.telemetry", level="INFO") as captured:
            await sink.play(plan(1), threading.Event())
        stages = [json.loads(entry.getMessage())["stage"] for entry in captured.records]
        self.assertNotIn("output_played", stages)
        self.assertIn("output_dropped", stages)


if __name__ == "__main__":
    unittest.main()
