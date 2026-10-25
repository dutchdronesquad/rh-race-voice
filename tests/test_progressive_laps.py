"""Exercise early cached speech through the actual preparation/output planners."""

# ruff: noqa: PT009, SLF001

from __future__ import annotations

import asyncio
import json
import threading
import time
import unittest
from dataclasses import replace
from unittest.mock import AsyncMock, Mock

from sendspin_service.race.race_planner import (
    CalloutPlan,
    PlaybackPlanner,
    PreparationPlanner,
    SendspinPlaybackSink,
)
from sendspin_service.race.race_protocol import EventKind
from sendspin_service.race.speech import SpeechEngine
from tests.test_race_planner import plan, until, wav_audio
from tests.test_service_speech import SETTINGS


class ProgressiveLapTests(unittest.IsolatedAsyncioTestCase):
    """A cached callsign may play while synthesis of its time is still blocked."""

    async def asyncSetUp(self) -> None:
        """Keep synthesis and playback controlled without model/network dependencies."""
        self.release = asyncio.Event()
        self.started = asyncio.Event()
        self.cached = True
        self.warm = True
        self.fail = False
        self.current = True
        self.prefix = (wav_audio(0.4), wav_audio(0.6))
        self.tail = wav_audio(0.8)
        self.worker = Mock()
        self.worker.request = AsyncMock(side_effect=self.generate)
        self.backend = Mock()
        self.backend.play.return_value = True
        self.sink = SendspinPlaybackSink(self.backend, destination="local")
        self.output = PlaybackPlanner(
            self.sink, is_current=lambda _: self.current, destination="local"
        )
        self.plans = []

        def ready(callout: CalloutPlan) -> None:
            self.plans.append(callout)
            self.output.submit(callout)

        self.preparation = PreparationPlanner(
            SpeechEngine(self.worker),
            assets={"stage": b"beep"},
            is_current=lambda _: self.current,
            ready=ready,
        )
        self.addAsyncCleanup(self.output.close)
        self.addAsyncCleanup(self.preparation.close)

    async def generate(self, payload: dict, **_kwargs: object) -> dict:
        """Block only the dynamic time; reusable audio can come from RAM or disk."""
        subdir = payload["subdir"]
        if subdir == "tmp":
            self.started.set()
            await self.release.wait()
            if self.fail:
                raise RuntimeError("Time synthesis failed")
            audio = self.tail
        else:
            audio = self.prefix[int(subdir == "precache/laps")]
        return {
            "audio": audio,
            "memory_cache_hit": self.cached,
            "model_warm": self.warm,
            "cache_hit": True,
        }

    def submit(self, number: int = 1, *, pilot: int = 1) -> None:
        """Ensure each lap includes a pilot and the dynamic phonetic time."""
        callout = plan(number, pilot=pilot)
        callout = replace(
            callout,
            event=replace(callout.event, pilot_name="Klaas", text="twenty seconds"),
            audio=(),
        )
        self.preparation.submit(callout, SETTINGS)

    def played_audio(self) -> list[tuple[bytes, ...]]:
        """Inspect complete backend submissions, not scheduler private task order."""
        return [
            tuple(clip.data for clip in call.args[0])
            for call in self.backend.play.call_args_list
        ]

    async def test_cached_prefix_reaches_playback_before_time_finishes(self) -> None:
        """The prefix removes neural synthesis from time to first playback request."""
        with self.assertLogs("sendspin_service.telemetry", level="INFO") as captured:
            self.submit()
            await until(lambda: self.backend.play.call_count == 1)
            self.assertEqual(self.played_audio(), [self.prefix])
            self.assertFalse(self.release.is_set())
            self.assertFalse(self.plans[0].completion.done())
            self.release.set()
            await until(lambda: self.backend.play.call_count == 2)
            await until(lambda: self.output._active is None)
        self.assertEqual(self.played_audio(), [self.prefix, (self.tail,)])
        self.assertEqual(len(self.plans), 1)
        stages = [
            json.loads(record.getMessage())["stage"] for record in captured.records
        ]
        self.assertEqual(stages.count("output_played"), 1)
        self.assertNotIn("output_dropped", stages)
        self.assertEqual(self.plans[0].retained_audio, (*self.prefix, self.tail))
        self.assertAlmostEqual(self.plans[0].duration_s, 1.8)

    async def test_cold_prefix_waits_for_the_complete_announcement(self) -> None:
        """No optimistic prefix is exposed without the prepared RAM audio."""
        self.cached = False
        self.submit()
        await self.started.wait()
        await until(lambda: self.worker.request.call_count == 3)
        self.assertEqual(self.plans, [])
        self.release.set()
        await until(lambda: self.backend.play.call_count == 1)
        self.assertEqual(self.played_audio(), [(*self.prefix, self.tail)])
        self.assertIsNone(self.plans[0].completion)

    async def test_cold_model_does_not_expose_prefix_before_time_is_ready(self) -> None:
        """Cached audio alone cannot hide a potentially long model startup pause."""
        self.warm = False
        self.submit()
        await self.started.wait()
        await until(lambda: self.worker.request.call_count == 3)
        self.assertEqual(self.plans, [])
        self.assertEqual(self.backend.play.call_count, 0)
        self.release.set()
        await until(lambda: self.backend.play.call_count == 1)
        self.assertEqual(self.played_audio(), [(*self.prefix, self.tail)])
        self.assertIsNone(self.plans[0].completion)

    async def test_another_pilot_cannot_interleave_with_the_reserved_time(self) -> None:
        """Another pilot must wait until the first callout's time is queued."""
        self.submit()
        await until(lambda: self.backend.play.call_count == 1)
        self.output.submit(replace(plan(2, pilot=2), audio=(b"other pilot",)))
        self.release.set()
        await until(lambda: self.backend.play.call_count == 3)
        self.assertEqual(
            self.played_audio(), [self.prefix, (self.tail,), (b"other pilot",)]
        )

    async def test_new_lap_cannot_cancel_the_time_after_its_prefix_is_published(
        self,
    ) -> None:
        """Keep an admitted callout complete even when this pilot crosses again."""
        self.submit()
        await until(lambda: self.backend.play.call_count == 1)
        self.submit(2)
        self.release.set()
        await until(lambda: self.preparation._active is None)
        await until(lambda: self.output._active is None)
        self.assertEqual(self.played_audio()[1], (self.tail,))
        self.assertTrue(self.plans[0].completion.result())

    async def test_stop_during_time_synthesis_never_appends_stale_time(self) -> None:
        """The shared completion must finish empty when a stop invalidates synthesis."""
        self.submit()
        await until(lambda: self.backend.play.call_count == 1)
        self.current = False
        self.preparation.invalidate()
        await self.output.flush()
        await until(lambda: self.output._active is None)
        self.release.set()
        self.assertEqual(self.played_audio(), [self.prefix])
        self.assertEqual(self.plans[0].completion.result(), ())
        self.backend.stop.assert_called()

    async def test_failed_time_releases_the_output_slot_without_false_success(
        self,
    ) -> None:
        """An audible prefix is not a successfully delivered complete lap callout."""
        with (
            self.assertLogs("sendspin_service.telemetry", level="INFO") as captured,
            self.assertLogs("sendspin_service.race.race_planner", level="ERROR"),
        ):
            self.submit()
            await until(lambda: self.backend.play.call_count == 1)
            self.fail = True
            self.release.set()
            await until(lambda: self.preparation._active is None)
            await until(lambda: self.output._active is None)
        stages = [
            json.loads(record.getMessage())["stage"] for record in captured.records
        ]
        self.assertEqual(stages.count("output_dropped"), 1)
        self.assertNotIn("output_played", stages)
        self.assertEqual(self.played_audio(), [self.prefix])

    async def test_expired_continuation_is_not_appended(self) -> None:
        """A prefix never grants the time a fresh deadline."""
        completion = asyncio.get_running_loop().create_future()
        callout = replace(
            plan(1),
            audio=self.prefix,
            completion=completion,
            deadline=time.monotonic() + 0.05,
        )
        await self.sink.play(callout, threading.Event())
        self.assertFalse(completion.cancelled())
        self.assertEqual(self.backend.play.call_count, 1)

    async def test_one_output_cannot_cancel_shared_synthesis(self) -> None:
        """Cancellation of a waiting output leaves the shared completion usable."""
        completion = asyncio.get_running_loop().create_future()
        callout = replace(plan(1), audio=self.prefix, completion=completion)
        waiting = asyncio.create_task(callout.complete_audio())
        await asyncio.sleep(0)
        waiting.cancel()
        with self.assertRaises(asyncio.CancelledError):  # noqa: PT027
            await waiting
        self.assertFalse(completion.cancelled())
        completion.set_result((*self.prefix, self.tail))
        self.assertEqual(await callout.complete_audio(), (*self.prefix, self.tail))

    async def test_race_signal_preempts_prefix_and_discards_pending_time(self) -> None:
        """A reserved callout cannot delay staging tones or revive afterwards."""
        self.submit()
        await until(lambda: self.backend.play.call_count == 1)
        self.preparation.submit(plan(2, EventKind.TONE), SETTINGS)
        await until(lambda: self.backend.play.call_count == 2)
        self.release.set()
        await until(lambda: self.output._active is None)
        self.assertEqual(self.played_audio(), [self.prefix, (b"beep",)])
        self.assertEqual(self.plans[0].completion.result(), ())
