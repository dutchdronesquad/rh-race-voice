"""Preserve callout semantics while moving orchestration outside RotorHazard."""

# ruff: noqa: PT009

from __future__ import annotations

import time
import unittest
from dataclasses import replace
from unittest.mock import AsyncMock

from sendspin_service.race.race_protocol import EventKind, RaceEvent
from sendspin_service.race.speech import SpeechEngine
from tests.test_race_protocol import event_payload

SETTINGS = {"model": "nl_NL-pim-medium", "speed": 1, "noise": 0.667, "noise_w": 0.8}


class ServiceSpeechTests(unittest.IsolatedAsyncioTestCase):
    """Use the real phrase planners with a controlled synthesis transport."""

    def setUp(self) -> None:
        """Supply an already-admitted event and a worker returning pinned bytes."""
        self.worker = AsyncMock()
        self.worker.request.return_value = {"audio": b"wav", "cache_hit": True}
        self.engine = SpeechEngine(self.worker)
        self.event = RaceEvent.parse(event_payload())

    async def test_lap_retains_name_localized_number_and_phonetic_time(self) -> None:
        """Routing identity is independent from the spoken-name segment."""
        audio = await self.engine.synthesize(
            self.event,
            SETTINGS,
            deadline=time.monotonic() + 5,
            priority=3,
            is_current=lambda: True,
        )
        self.assertEqual(audio, (b"wav",) * 3)
        self.assertEqual(
            [call.args[0]["text"] for call in self.worker.request.call_args_list],
            ["Klaas,", "Ronde 4", "twenty three point four five"],
        )

    async def test_live_countdowns_reuse_manually_prepared_requests(self) -> None:
        """Both race-clock and schedule phrases use identical live cache keys."""
        async for _ in self.engine.prepare(SETTINGS, [], is_current=lambda: True):
            pass
        prepared = [
            call.args[0]
            for call in self.worker.request.call_args_list
            if call.args[0]["subdir"] == "precache/clock"
        ]
        self.assertGreater(len(prepared), 5)
        for request in prepared:
            self.worker.reset_mock()
            await self.engine.synthesize(
                replace(self.event, kind=EventKind.COUNTDOWN, text=request["text"]),
                SETTINGS,
                deadline=time.monotonic() + 5,
                priority=0,
                is_current=lambda: True,
            )
            self.assertEqual(self.worker.request.call_args.args[0], request)

    async def test_tone_never_calls_worker(self) -> None:
        """Signals stay actionable during a stopped/hung Piper process."""
        event = replace(self.event, kind=EventKind.TONE, asset="stage", text=None)
        self.assertEqual(
            await self.engine.synthesize(
                event,
                SETTINGS,
                deadline=time.monotonic() + 5,
                priority=-1,
                is_current=lambda: True,
            ),
            (),
        )
        self.worker.request.assert_not_called()

    async def test_cancelled_generation_does_not_return_partial_speech(self) -> None:
        """An old result arriving after a stop cannot reach playback."""
        current = True

        async def complete_after_stop(*_args, **_kwargs):  # noqa: ANN002, ANN003, ANN202
            nonlocal current
            current = False
            return {"audio": b"old"}

        self.worker.request.side_effect = complete_after_stop
        self.assertEqual(
            await self.engine.synthesize(
                self.event,
                SETTINGS,
                deadline=time.monotonic() + 5,
                priority=3,
                is_current=lambda: current,
            ),
            (),
        )
        self.worker.request.assert_awaited_once()

    async def test_preparation_is_manual_reuses_cache_and_stops_between_phrases(
        self,
    ) -> None:
        """Never clear reusable audio or queue the entire preparation batch."""
        self.worker.request.assert_not_called()
        current = True
        progress = []
        async for state in self.engine.prepare(
            SETTINGS,
            ["Klaas"],
            is_current=lambda: current,  # noqa: B023
        ):
            progress.append(state)
            if len(progress) == 2:
                current = False
        self.assertEqual(progress[-1]["generated"], 0)
        self.assertEqual(progress[-1]["reused"], 2)
        self.assertEqual(self.worker.request.await_count, 2)
        self.assertTrue(
            all(
                call.args[0]["operation"] == "synthesize"
                and call.kwargs["priority"] == 10
                for call in self.worker.request.call_args_list
            )
        )


if __name__ == "__main__":
    unittest.main()
