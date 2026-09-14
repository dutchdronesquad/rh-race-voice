"""Harness template: a dense lap burst proving telemetry survives eviction.

Run RaceIngest through real HTTP admission, preparation and playback while a
fake synthesis worker stays blocked, forcing capacity eviction in the bounded
preparation queue. Every event that was "received" must end with exactly one
terminal telemetry record (output_played, or a drop/disabled/non-accepted
admission), and no event may be marked output_played more than once. Future
deterministic scenarios (heat changes, database replacement, manual
preparation, stop, late joins, selection changes, expired work) should follow
this same shape: real HTTP admission, a controllable fake worker, and an
assertion over the captured telemetry trail rather than internal state.
"""

# ruff: noqa: PT009

from __future__ import annotations

import asyncio
import time
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import Mock, patch

from aiohttp.test_utils import TestClient, TestServer

from sendspin_service.server import SendspinService, ServiceConfig, _create_app
from tools.latency_report import parse_line

VOICE = {
    "model": "en_GB-alan-medium",
    "speed": 1,
    "noise": 0.667,
    "noise_w": 0.8,
    "enabled": True,
    "callout_flags": {},
}
PILOT_COUNT = 8


class Worker:
    """Hold inference until released, mirroring the RaceIngest test double."""

    def __init__(self) -> None:
        """Default to immediate completion; the test pauses it explicitly."""
        self.started = asyncio.Event()
        self.release = asyncio.Event()
        self.release.set()
        self.calls = []
        self.closed = False

    async def request(self, payload: dict, **_kwargs: object) -> dict:
        """Simulate the supervisor boundary without loading a model in tests."""
        self.calls.append(payload)
        self.started.set()
        await self.release.wait()
        return {"audio": b"RIFF-stub", "cache_hit": False, "duration_ms": 5}

    async def close(self) -> None:
        """Record application cleanup."""
        self.closed = True


async def until(predicate) -> None:  # noqa: ANN001
    """Wait for background planning without imposing hardware timing thresholds."""
    async with asyncio.timeout(2):
        while not predicate():  # noqa: ASYNC110
            await asyncio.sleep(0.001)


class DenseLapBurstScenarioTests(unittest.IsolatedAsyncioTestCase):
    """Prove correlation telemetry survives a burst faster than synthesis."""

    async def asyncSetUp(self) -> None:
        """Run an isolated HTTP listener with one blocked synthesis worker."""
        self.root = Path(self.enterContext(TemporaryDirectory()))
        self.worker = Worker()
        self.backend = Mock()
        self.backend.connected_client_count.return_value = 0
        self.enterContext(
            patch("sendspin_service.server.SendSpinServer", return_value=self.backend)
        )
        self.enterContext(
            patch(
                "sendspin_service.synthesis.SynthesisWorker", return_value=self.worker
            )
        )
        self.service = SendspinService(
            ServiceConfig(race_cache_dir=self.root, api_token="test")  # noqa: S106
        )
        self.client = TestClient(
            TestServer(_create_app(self.service)),
            headers={"Authorization": "Bearer test"},
        )
        await self.client.start_server()
        self.addAsyncCleanup(self.client.close)
        owner = await (await self.client.get("/v2/session")).json()
        opened = await (
            await self.client.post(
                "/v2/session",
                json={**owner, "epoch": "rh-process", "nonce": "request-1"},
            )
        ).json()
        self.session_id = opened["session_id"]
        self.snapshot = {
            "version": "race-events/1",
            "session_id": self.session_id,
            "context": {
                "competition_id": "race-day",
                "revision": 1,
                "generation": 0,
                "heat_id": 1,
            },
            "heat_name": "Heat 1",
            "pilots": [],
            "voice": dict(VOICE),
        }
        self.assertEqual(
            (await self.client.put("/v2/state", json=self.snapshot)).status, 200
        )
        await self._synchronize_clock()
        self.backend.reset_mock()

    async def _synchronize_clock(self) -> None:
        response = await self.client.post(
            "/v2/clock", json={"session_id": self.session_id, "sent": time.monotonic()}
        )
        probe = await response.json()
        response = await self.client.post(
            "/v2/clock",
            json={
                "session_id": self.session_id,
                "probe_id": probe["probe_id"],
                "received": time.monotonic(),
            },
        )
        self.assertEqual(response.status, 200, await response.text())

    def _lap_event(self, sequence: int, pilot_id: int) -> dict:
        now = time.monotonic()
        return {
            "version": "race-events/1",
            "session_id": self.session_id,
            "event_id": f"{self.session_id}:{sequence}",
            "sequence": sequence,
            "context": dict(self.snapshot["context"]),
            "kind": "lap",
            "occurred_at": now,
            "expires_at": now + 10,
            "payload": {
                "text": "one thirty two",
                "pilot_id": pilot_id,
                "pilot_name": f"Pilot {pilot_id}",
                "lap": 2,
            },
        }

    async def test_dense_lap_burst_keeps_every_event_in_the_telemetry_trail(
        self,
    ) -> None:
        """A burst faster than synthesis must evict, not silently drop, events."""
        event_ids = {
            f"{self.session_id}:{sequence}" for sequence in range(1, PILOT_COUNT + 1)
        }
        self.worker.release.clear()
        with self.assertLogs("sendspin_service.telemetry", level="INFO") as captured:
            first = self._lap_event(1, pilot_id=1)
            self.assertEqual(
                (await self.client.post("/v2/events", json=first)).status, 202
            )
            await asyncio.wait_for(self.worker.started.wait(), 2)
            # Submit the rest of the burst while the worker is still blocked on
            # the first lap, forcing the bounded preparation queue (max 4
            # pending laps) to evict older pending pilots.
            for sequence in range(2, PILOT_COUNT + 1):
                event = self._lap_event(sequence, pilot_id=sequence)
                response = await self.client.post("/v2/events", json=event)
                self.assertEqual(response.status, 202)
            self.worker.release.set()
            await until(lambda: self.backend.play.call_count == 5)

        records = [
            record
            for record in (parse_line(entry.getMessage()) for entry in captured.records)
            if record is not None
        ]
        by_event: dict[str, list[dict]] = {}
        for record in records:
            by_event.setdefault(record["event_id"], []).append(record)

        # Every submitted event must have been "received"; none is missing
        # from the telemetry trail entirely.
        self.assertEqual(event_ids - set(by_event), set())

        played_counts: dict[str, int] = {}
        for event_id in event_ids:
            stages = [record["stage"] for record in by_event[event_id]]
            admission_outcomes = [
                record.get("outcome")
                for record in by_event[event_id]
                if record["stage"] == "admission"
            ]
            played_counts[event_id] = stages.count("output_played")
            terminal = (
                "output_played" in stages
                or "output_dropped" in stages
                or "dropped" in stages
                or "disabled" in stages
                or any(
                    outcome not in (None, "accepted") for outcome in admission_outcomes
                )
            )
            self.assertTrue(
                terminal,
                f"event {event_id} has no terminal telemetry record: {stages}",
            )

        self.assertTrue(all(count <= 1 for count in played_counts.values()))
        # The bounded preparation queue (1 active + 4 pending) cannot hold all
        # 8 laps at once, so some pilots must have been evicted rather than
        # silently played twice or lost.
        self.assertEqual(sum(played_counts.values()), 5)
        self.assertLess(sum(played_counts.values()), len(event_ids))


if __name__ == "__main__":
    unittest.main()
