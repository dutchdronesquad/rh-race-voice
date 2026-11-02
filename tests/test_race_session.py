"""Replay complete races through real HTTP, worker pipes and output planners."""

# ruff: noqa: PT009, SLF001

from __future__ import annotations

import asyncio
import json
import threading
import time
import unittest
from collections import Counter
from pathlib import Path
from tempfile import TemporaryDirectory

from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from sendspin_service.race.race_ingest import RaceIngest, add_routes
from sendspin_service.race.race_planner import Destination, SendspinPlaybackSink
from sendspin_service.synthesis.synthesis import SynthesisWorker
from tests.test_race_planner import wav_audio

VOICE = {
    "model": "en_GB-alan-medium",
    "speed": 1,
    "noise": 0.667,
    "noise_w": 0.8,
    "enabled": True,
    "callout_flags": {},
}
TERMINAL = {"output_played", "output_dropped", "output_filtered"}


class ControlledWorker(SynthesisWorker):
    """Keep real supervisor/cache/pipe behavior with fake WAV generation only."""

    def __init__(self, root: Path) -> None:
        """Use existing probe protocol rather than downloading or loading a model."""
        super().__init__(root, worker_module="tests.helpers.synthesis_worker_probe")
        self.release = asyncio.Event()
        self.release.set()
        self.started = asyncio.Event()
        self.crash_next = False

    async def _execute(self, request: bytes) -> dict:
        payload = json.loads(request)
        if payload.get("operation") == "synthesize" and payload.get("subdir") == "tmp":
            self.started.set()
            await self.release.wait()
            if self.crash_next:
                self.crash_next = False
                payload["crash"] = True
        payload["mock_audio"] = True
        return await super()._execute((json.dumps(payload) + "\n").encode())


class Backend:
    """Accept immutable WAVs without opening a player, port or audio device."""

    def __init__(self) -> None:
        """Let tests block playback independently from synthesis."""
        self.release = threading.Event()
        self.release.set()
        self.entered = threading.Event()
        self.queued: list[str] = []

    def play(self, clips, deadline, _target, _volume, *, cancelled) -> bool:  # noqa: ANN001
        """Observe cancellation while a test deliberately holds a backend call."""
        self.entered.set()
        if not self.release.wait(3):
            raise TimeoutError("Test did not release playback")
        if cancelled.is_set() or time.monotonic() >= deadline:
            return False
        self.queued.extend(clip.name for clip in clips)
        return True

    def stop(self, *, strict: bool = False) -> None:  # noqa: ARG002
        """Release any held backend call after the planner marks it cancelled."""
        self.release.set()


class RaceSessionTests(unittest.IsolatedAsyncioTestCase):
    """Assert event conservation throughout a complete race and its transitions."""

    async def asyncSetUp(self) -> None:
        """Connect one race source to independent full and selected outputs."""
        root = Path(self.enterContext(TemporaryDirectory()))
        models = root / "models"
        models.mkdir()
        (models / f"{VOICE['model']}.onnx").write_bytes(b"fake model revision")
        (models / f"{VOICE['model']}.onnx.json").write_text("{}")
        self.worker = ControlledWorker(root)
        self.local, self.selected = Backend(), Backend()
        self.ingest = RaceIngest(
            self.worker,
            {
                "local": Destination(
                    SendspinPlaybackSink(self.local, destination="local")
                ),
                "selected": Destination(
                    SendspinPlaybackSink(self.selected, destination="selected"),
                    accepts=lambda p: (
                        p.event.pilot_id is None or p.event.pilot_id % 2 == 0
                    ),
                ),
            },
            {"stage": wav_audio(0.01), "buzzer": wav_audio(0.01)},
        )
        app = web.Application()
        add_routes(app, self.ingest)

        async def cleanup(_app: web.Application) -> None:
            await self.ingest.close()

        app.on_cleanup.append(cleanup)
        self.client = TestClient(TestServer(app))
        await self.client.start_server()
        self.addAsyncCleanup(self.client.close)
        self.sequence = self.command_sequence = 0
        self.events: dict[str, dict] = {}
        self.parsed: list[dict] = []
        owner = await (await self.client.get("/v2/session")).json()
        owner = await (
            await self.client.post(
                "/v2/session", json={**owner, "epoch": "scenario", "nonce": "start"}
            )
        ).json()
        self.session = owner["session_id"]
        self.snapshot = {
            "version": "race-events/1",
            "session_id": self.session,
            "context": {
                "competition_id": "scenario",
                "revision": 1,
                "generation": 0,
                "heat_id": 1,
            },
            "heat_name": "Heat 1",
            "pilots": [],
            "voice": dict(VOICE),
        }
        await self._state()
        probe = await (
            await self.client.post(
                "/v2/clock", json={"session_id": self.session, "sent": time.monotonic()}
            )
        ).json()
        response = await self.client.post(
            "/v2/clock",
            json={
                "session_id": self.session,
                "probe_id": probe["probe_id"],
                "received": time.monotonic(),
            },
        )
        self.assertEqual(response.status, 200)

    async def _state(self, *, heat: int | None = None, stop: bool = False) -> None:
        if heat is not None or stop:
            self.snapshot["context"]["revision"] += 1
            self.snapshot["context"]["generation"] += 1
        if heat is not None:
            self.snapshot["context"]["heat_id"] = heat
        response = await self.client.put("/v2/state", json=self.snapshot)
        self.assertEqual(response.status, 200, await response.text())

    async def _send(self, kind: str, payload: dict, *, lifetime: float = 10) -> str:
        self.sequence += 1
        event_id = f"{self.session}:{self.sequence}"
        now = time.monotonic()
        event = {
            "version": "race-events/1",
            "session_id": self.session,
            "event_id": event_id,
            "sequence": self.sequence,
            "context": dict(self.snapshot["context"]),
            "kind": kind,
            "occurred_at": now,
            "expires_at": now + lifetime,
            "payload": payload,
        }
        response = await self.client.post("/v2/events", json=event)
        self.assertEqual(response.status, 202, await response.text())
        self.events[event_id] = event
        return event_id

    async def _lap(self, pilot: int, lap: int, *, lifetime: float = 10) -> str:
        return await self._send(
            "lap",
            {
                "pilot_id": pilot,
                "pilot_name": f"Pilot {pilot}",
                "lap": lap,
                "text": f"{lap}.{self.sequence} seconds",
            },
            lifetime=lifetime,
        )

    def _records(self) -> list[dict]:
        self.parsed.extend(
            json.loads(r.getMessage())
            for r in self.captured.records[len(self.parsed) :]
        )
        return self.parsed

    def _finals(self, event_id: str) -> list[dict]:
        return [
            r
            for r in self._records()
            if r["event_id"] == event_id and r["stage"] in TERMINAL
        ]

    async def _settled(self, event_ids: list[str]) -> None:
        async with asyncio.timeout(3):
            while True:
                if all(
                    any("destination" not in r for r in self._finals(event_id))
                    or {r.get("destination") for r in self._finals(event_id)}
                    == {"local", "selected"}
                    for event_id in event_ids
                ):
                    return
                await asyncio.sleep(0.001)

    async def _prepare(self) -> None:
        self.command_sequence += 1
        command_id = f"{self.session}:command:{self.command_sequence}"
        response = await self.client.post(
            "/v2/commands",
            json={
                "version": "race-events/1",
                "session_id": self.session,
                "command_id": command_id,
                "operation": "prepare",
                "context": self.snapshot["context"],
                "settings_revision": self.snapshot["context"]["revision"],
            },
        )
        self.assertEqual(response.status, 200, await response.text())
        async with asyncio.timeout(3):
            while True:
                result = await (
                    await self.client.get(f"/v2/commands/{command_id}")
                ).json()
                if result["status"] not in {"queued", "running"}:
                    self.assertEqual(result["status"], "completed", result)
                    return
                await asyncio.sleep(0.001)

    def _audit(self) -> None:
        """Require exact final ownership, explanations and no duplicate queued clips."""
        for event_id, event in self.events.items():
            received = next(
                r
                for r in self._records()
                if r["event_id"] == event_id and r["stage"] == "received"
            )
            self.assertEqual(received["pilot_id"], event["payload"].get("pilot_id"))
            self.assertEqual(received["heat_id"], event["context"]["heat_id"])
            self.assertEqual(received["generation"], event["context"]["generation"])
            records = self._finals(event_id)
            global_records = [r for r in records if "destination" not in r]
            if global_records:
                self.assertEqual(len(records), 1, (event_id, records))
                self.assertEqual(records[0]["stage"], "output_dropped")
            else:
                self.assertEqual(
                    Counter(r["destination"] for r in records),
                    {"local": 1, "selected": 1},
                )
                if event["kind"] == "lap" and event["payload"]["pilot_id"] % 2:
                    selected = next(
                        r for r in records if r["destination"] == "selected"
                    )
                    self.assertEqual(selected["stage"], "output_filtered")
            for record in records:
                if record["stage"] == "output_played":
                    self.assertEqual(record["delivery"], "queued")
                    backend = (
                        self.local
                        if record["destination"] == "local"
                        else self.selected
                    )
                    clips = [
                        name
                        for name in backend.queued
                        if name.startswith(event_id + "-")
                    ]
                    self.assertEqual(len(clips), 3 if event["kind"] == "lap" else 1)
                else:
                    self.assertTrue(record.get("reason"), (event_id, record))
        for backend in (self.local, self.selected):
            self.assertTrue(
                all(count == 1 for count in Counter(backend.queued).values())
            )

    async def _race(self, pilot_count: int) -> None:
        self.snapshot["pilots"] = [
            {"pilot_id": p, "callsign": f"Pilot {p}", "spoken_name": f"Pilot {p}"}
            for p in range(1, pilot_count + 1)
        ]
        self.snapshot["context"]["revision"] += 1
        await self._state()
        with self.assertLogs(
            "sendspin_service.telemetry", level="INFO"
        ) as self.captured:
            for lap in range(1, 7):
                if lap == 2:
                    await self._prepare()
                self.worker.started.clear()
                self.worker.release.clear()
                ids = [await self._lap(1, lap)]
                await asyncio.wait_for(self.worker.started.wait(), 3)
                ids.extend([await self._lap(p, lap) for p in range(2, pilot_count + 1)])
                if lap == 3:
                    duplicate = await self.client.post(
                        "/v2/events", json=self.events[ids[0]]
                    )
                    self.assertEqual((await duplicate.json())["outcome"], "duplicate")
                self.worker.release.set()
                await self._settled(ids)
                queued = [
                    r["event_id"]
                    for r in self._records()
                    if r["event_id"] in ids
                    and r["stage"] == "output_played"
                    and r.get("destination") == "local"
                ]
                self.assertEqual(
                    queued, ids if pilot_count == 4 else [ids[0], *ids[-4:]]
                )
                if lap > 1:
                    self.assertTrue(
                        any(
                            r["stage"] == "prefix_ready" and r["event_id"] == ids[0]
                            for r in self._records()
                        )
                    )
            buzzer = await self._send("tone", {"asset": "buzzer"})
            await self._settled([buzzer])
            winner = await self._send(
                "voice", {"text": "Winner is Pilot 1", "winner_flag": True}
            )
            await self._settled([winner])
            self.assertTrue(
                all(r["stage"] == "output_played" for r in self._finals(winner))
            )
            await self._transitions()
            self._audit()

    async def _transitions(self) -> None:
        """Overlap preparation/playback with heat changes, stop, expiry and recovery."""
        self.worker.started.clear()
        self.worker.release.clear()
        ids = [await self._lap(1, 7)]
        await asyncio.wait_for(self.worker.started.wait(), 3)
        ids.append(await self._lap(2, 7))
        await self._state(heat=2)
        await self._settled(ids)
        self.assertTrue(
            all(r.get("reason") == "heat_changed" for i in ids for r in self._finals(i))
        )
        self.worker.release.set()
        # Let the already-running abandoned worker operation finish before the
        # next controlled phase blocks a new one.
        async with asyncio.timeout(3):
            while self.worker._jobs:  # noqa: ASYNC110
                await asyncio.sleep(0.001)
        self.worker.started.clear()
        self.worker.release.clear()
        self.local.entered.clear()
        self.local.release.clear()
        active = await self._lap(2, 2)
        await asyncio.wait_for(self.worker.started.wait(), 3)
        async with asyncio.timeout(3):
            while not self.local.entered.is_set():  # noqa: ASYNC110
                await asyncio.sleep(0.001)
        waiting = await self._lap(3, 2)
        await self._state(stop=True)
        await self._settled([active, waiting])
        self.assertTrue(
            all(
                r.get("reason") == "state_changed"
                for i in (active, waiting)
                for r in self._finals(i)
            )
        )
        self.worker.release.set()
        async with asyncio.timeout(3):
            while self.worker._jobs:  # noqa: ASYNC110
                await asyncio.sleep(0.001)
        self.worker.started.clear()
        self.worker.release.clear()
        blocking = await self._lap(1, 3)
        await asyncio.wait_for(self.worker.started.wait(), 3)
        expiring = await self._lap(2, 3, lifetime=0.03)
        await asyncio.sleep(0.05)
        self.worker.release.set()
        await self._settled([blocking, expiring])
        self.assertEqual([r["reason"] for r in self._finals(expiring)], ["expired"])
        await self._state(stop=True)
        fresh = await self._send("tone", {"asset": "stage"})
        await self._settled([fresh])
        await self._recover_and_shutdown()

    async def _recover_and_shutdown(self) -> None:
        """Explain a worker crash, recover with explicit preparation, then close."""
        self.worker.started.clear()
        self.worker.release.clear()
        self.worker.crash_next = True
        crashing = await self._lap(2, 4)
        await asyncio.wait_for(self.worker.started.wait(), 3)
        async with asyncio.timeout(3):
            while not any(  # noqa: ASYNC110
                r["stage"] == "prefix_ready" and r["event_id"] == crashing
                for r in self._records()
            ):
                await asyncio.sleep(0.001)
        with self.assertLogs("sendspin_service.race.race_planner", level="ERROR"):
            self.worker.release.set()
            await self._settled([crashing])
            async with asyncio.timeout(3):
                while self.ingest._preparation._active is not None:  # noqa: ASYNC110
                    await asyncio.sleep(0.001)
        self.assertTrue(
            all(r["stage"] == "output_dropped" for r in self._finals(crashing))
        )
        await self._prepare()
        recovered = await self._lap(2, 4)
        await self._settled([recovered])
        self.assertTrue(
            all(r["stage"] == "output_played" for r in self._finals(recovered))
        )
        self.worker.started.clear()
        self.worker.release.clear()
        closing = [await self._lap(2, 99)]
        await asyncio.wait_for(self.worker.started.wait(), 3)
        closing.append(await self._lap(3, 99))
        await self.client.close()
        await self._settled(closing)
        self.assertTrue(
            all(r.get("reason") == "shutdown" for i in closing for r in self._finals(i))
        )

    async def test_complete_four_pilot_race(self) -> None:
        """All four pilots survive ordinary bursts across six rounds and boundaries."""
        await self._race(4)

    async def test_complete_eight_pilot_race_with_explained_overload(self) -> None:
        """Larger bursts stay bounded and every intentional loss has one reason."""
        await self._race(8)
