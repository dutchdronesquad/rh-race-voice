"""Require one explained final outcome across planner cancellation races."""

# ruff: noqa: PT009, SLF001

from __future__ import annotations

import asyncio
import json
import unittest
from dataclasses import replace
from unittest.mock import AsyncMock, Mock

from sendspin_service.race.race_planner import (
    PlaybackPlanner,
    PreparationPlanner,
    record_output,
)
from sendspin_service.race.race_protocol import EventKind
from tests.test_race_planner import plan, until


def outcomes(captured) -> list[dict]:  # noqa: ANN001
    """Ignore intermediate timing records when auditing final event ownership."""
    records = [json.loads(record.getMessage()) for record in captured.records]
    return [r for r in records if r["stage"] in {"output_played", "output_dropped"}]


class PreparationOutcomeTests(unittest.IsolatedAsyncioTestCase):
    """Pending, active and not-yet-started preparation must all be accounted for."""

    async def asyncSetUp(self) -> None:
        """Hold inference without loading a voice model."""
        self.entered = asyncio.Event()
        self.release = asyncio.Event()
        self.speech = Mock()

        async def synthesize(*_args: object, **_kwargs: object) -> tuple[bytes, ...]:
            self.entered.set()
            await self.release.wait()
            return (b"speech",)

        self.speech.synthesize = AsyncMock(side_effect=synthesize)
        self.ready = Mock()
        self.current = True
        self.planner = PreparationPlanner(
            self.speech,
            assets={"stage": b"beep"},
            is_current=lambda _: self.current,
            ready=self.ready,
        )
        self.addAsyncCleanup(self.planner.close)

    async def test_signal_accounts_for_active_and_pending_laps_once(self) -> None:
        """Signal priority must explain both interrupted and discarded work."""
        with self.assertLogs("sendspin_service.telemetry", level="INFO") as captured:
            self.planner.submit(plan(1, pilot=1), {})
            await self.entered.wait()
            self.planner.submit(plan(2, pilot=2), {})
            self.planner.submit(plan(3, EventKind.TONE), {})
            await until(lambda: self.planner._active is None)
        records = outcomes(captured)
        self.assertEqual(len(records), 2)
        self.assertEqual({r["reason"] for r in records}, {"preempted", "superseded"})

    async def test_shutdown_accounts_for_all_admitted_preparation(self) -> None:
        """Closing a service must not silently forget queued inference."""
        with self.assertLogs("sendspin_service.telemetry", level="INFO") as captured:
            self.planner.submit(plan(1, pilot=1), {})
            await self.entered.wait()
            self.planner.submit(plan(2, pilot=2), {})
            await self.planner.close()
        records = outcomes(captured)
        self.assertEqual(len(records), 2)
        self.assertTrue(all(r["reason"] == "shutdown" for r in records))

    async def test_expired_waiting_preparation_is_reported_during_pruning(self) -> None:
        """Queue maintenance owns the terminal record for an expired item."""
        with self.assertLogs("sendspin_service.telemetry", level="INFO") as captured:
            self.planner.submit(plan(1, pilot=1), {})
            await self.entered.wait()
            self.planner.submit(plan(2, pilot=2), {})
            waiting = self.planner._pending[0]
            waiting.plan = replace(waiting.plan, deadline=0)
            self.planner.submit(plan(3, pilot=3), {})
            self.release.set()
            await until(lambda: self.ready.call_count == 2)
        self.assertEqual([r["reason"] for r in outcomes(captured)], ["expired"])


class PlaybackOutcomeTests(unittest.IsolatedAsyncioTestCase):
    """Output receipts suppress late replies independently for each destination."""

    async def output(
        self, name: str
    ) -> tuple[PlaybackPlanner, asyncio.Event, asyncio.Event]:
        """Keep a sink alive past stop to exercise the late-reply race."""
        entered, release = asyncio.Event(), asyncio.Event()
        sink = Mock()

        async def play(callout, _cancelled) -> None:  # noqa: ANN001
            entered.set()
            await release.wait()
            record_output(callout, "output_played", destination=name)

        sink.play = AsyncMock(side_effect=play)
        sink.stop = AsyncMock()
        output = PlaybackPlanner(sink, is_current=lambda _: True, destination=name)
        self.addAsyncCleanup(output.close)
        return output, entered, release

    async def test_invalidation_records_pending_and_active_without_late_duplicates(
        self,
    ) -> None:
        """A late success after a heat change cannot overwrite cancellation."""
        output, entered, release = await self.output("local")
        with self.assertLogs("sendspin_service.telemetry", level="INFO") as captured:
            output.submit(plan(1, pilot=1))
            await entered.wait()
            output.submit(plan(2, pilot=2))
            await output.flush(reason="heat_changed")
            release.set()
            await until(lambda: output._active is None)
        records = outcomes(captured)
        self.assertEqual(len(records), 2)
        self.assertTrue(all(r["reason"] == "heat_changed" for r in records))
        self.assertTrue(all(r["stage"] == "output_dropped" for r in records))

    async def test_shared_audio_has_one_independent_outcome_per_destination(
        self,
    ) -> None:
        """One output can finish while another cancels the same prepared audio."""
        local, local_entered, local_release = await self.output("local")
        cloud, cloud_entered, cloud_release = await self.output("cloud")
        callout = plan(1)
        with self.assertLogs("sendspin_service.telemetry", level="INFO") as captured:
            local.submit(callout)
            cloud.submit(callout)
            await asyncio.gather(local_entered.wait(), cloud_entered.wait())
            local_release.set()
            await until(lambda: local._active is None)
            await cloud.flush(reason="state_changed")
            cloud_release.set()
            await until(lambda: cloud._active is None)
        records = outcomes(captured)
        self.assertEqual(len(records), 2)
        self.assertEqual(
            {(r["destination"], r["stage"]) for r in records},
            {("local", "output_played"), ("cloud", "output_dropped")},
        )
        self.assertEqual(
            next(r for r in records if r["destination"] == "local")["delivery"],
            "queued",
        )

    async def test_expired_pending_output_is_reported_before_next_admission(
        self,
    ) -> None:
        """Pruning expired playback keeps the event trace complete."""
        output, entered, release = await self.output("local")
        with self.assertLogs("sendspin_service.telemetry", level="INFO") as captured:
            output.submit(plan(1, pilot=1))
            await entered.wait()
            output.submit(plan(2, pilot=2))
            waiting = output._pending[0]
            waiting.plan = replace(waiting.plan, deadline=0)
            output.submit(plan(3, pilot=3))
            release.set()
            await until(lambda: output._active is None and not output._pending)
        records = outcomes(captured)
        self.assertEqual(len(records), 3)
        dropped = [r for r in records if r["stage"] == "output_dropped"]
        self.assertEqual([r["reason"] for r in dropped], ["expired"])
