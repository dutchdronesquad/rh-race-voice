"""Exercise the race-event HTTP path through real admission and audio planning."""

# ruff: noqa: PT009, PT027

from __future__ import annotations

import asyncio
import json
import time
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import Mock, patch

from aiohttp.test_utils import TestClient, TestServer

from sendspin_service.race.race_ingest import RaceIngest, logger
from sendspin_service.race.race_planner import (
    Destination,
    PreparationPlanner,
    SendspinPlaybackSink,
)
from sendspin_service.race.race_protocol import ClockMapping, ProtocolError
from sendspin_service.server import (
    DEFAULT_RACE_CACHE_DIR,
    SendspinService,
    ServiceConfig,
    _create_app,
)

ASSET = (
    Path(__file__).resolve().parents[1] / "custom_plugins/race_voice/assets/stage.wav"
)
VOICE = {
    "model": "en_GB-alan-medium",
    "speed": 1,
    "noise": 0.667,
    "noise_w": 0.8,
    "enabled": True,
    "callout_flags": {},
}


class Worker:
    """Hold inference independently of the HTTP loop and report actual WAV bytes."""

    def __init__(self) -> None:
        """Default to immediate completion; tests can pause individual requests."""
        self.started = asyncio.Event()
        self.release = asyncio.Event()
        self.release.set()
        self.calls = []
        self.closed = False

    async def request(self, payload: dict, **_kwargs: object) -> dict:
        """Simulate the supervisor boundary without loading a model in HTTP tests."""
        self.calls.append(payload)
        self.started.set()
        await self.release.wait()
        if payload["operation"] == "clear":
            return {"cleared": 3}
        return {"audio": ASSET.read_bytes(), "cache_hit": True}

    async def close(self) -> None:
        """Record application cleanup."""
        self.closed = True


async def until(predicate) -> None:  # noqa: ANN001
    """Wait for background planning without imposing hardware timing thresholds."""
    async with asyncio.timeout(2):
        while not predicate():  # noqa: ASYNC110
            await asyncio.sleep(0.001)


class RaceIngestTests(unittest.IsolatedAsyncioTestCase):
    """Keep real HTTP, snapshots, priorities and sink adaptation in the test path."""

    async def asyncSetUp(self) -> None:
        """Run an isolated HTTP listener; never connect real Sendspin clients."""
        self.root = Path(self.enterContext(TemporaryDirectory()))
        self.worker = Worker()
        self.backend = Mock()
        self.backend.connected_client_count.return_value = 0
        self.enterContext(
            patch("sendspin_service.server.SendSpinServer", return_value=self.backend)
        )
        self.enterContext(
            patch(
                "sendspin_service.synthesis.synthesis.SynthesisWorker",
                return_value=self.worker,
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
        self.open_request = {**owner, "epoch": "rh-process", "nonce": "request-1"}
        self.owner = await (
            await self.client.post("/v2/session", json=self.open_request)
        ).json()
        self.session_id = self.owner["session_id"]
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
        await self.synchronize()
        self.backend.reset_mock()

    async def synchronize(self, *, received: float | None = None) -> None:
        """Complete the actual four-timestamp HTTP exchange."""
        response = await self.client.post(
            "/v2/clock", json={"session_id": self.session_id, "sent": time.monotonic()}
        )
        probe = await response.json()
        response = await self.client.post(
            "/v2/clock",
            json={
                "session_id": self.session_id,
                "probe_id": probe["probe_id"],
                "received": received or time.monotonic(),
            },
        )
        self.assertEqual(response.status, 200, await response.text())

    def event(self, sequence: int = 1, *, kind: str = "voice") -> dict:
        """Build events using the snapshot captured by the producer."""
        now = time.monotonic()
        payload = {"asset": "stage"} if kind == "tone" else {"text": "Race ready"}
        return {
            "version": "race-events/1",
            "session_id": self.session_id,
            "event_id": f"{self.session_id}:{sequence}",
            "sequence": sequence,
            "context": dict(self.snapshot["context"]),
            "kind": kind,
            "occurred_at": now,
            "expires_at": now + 10,
            "payload": payload,
        }

    async def test_http_speech_reaches_direct_backend_once(self) -> None:
        """A retried event produces one synthesis and one playback, with its expiry."""
        event = self.event()
        response = await self.client.post("/v2/events", json=event)
        self.assertEqual(response.status, 202)
        await until(lambda: self.backend.play.called)
        retry = await self.client.post("/v2/events", json=event)
        self.assertEqual(await retry.json(), {"outcome": "duplicate"})
        self.assertEqual(len(self.worker.calls), 1)
        self.backend.play.assert_called_once()
        clips, deadline, target, volume = self.backend.play.call_args.args
        self.assertEqual(clips[0].data, ASSET.read_bytes())
        self.assertLessEqual(deadline, event["expires_at"] + 0.1)
        self.assertIsNone(target)
        self.assertEqual(volume, 1)

    async def test_tone_bypasses_blocked_lap_synthesis(self) -> None:
        """A beep is sent while Piper is still preparing a lap."""
        self.worker.release.clear()
        event = self.event(kind="lap")
        event["payload"] = {
            "text": "twenty seconds",
            "pilot_id": 7,
            "pilot_name": "Klaas",
            "lap": 2,
        }
        self.assertEqual((await self.client.post("/v2/events", json=event)).status, 202)
        await asyncio.wait_for(self.worker.started.wait(), 2)
        self.assertEqual(
            (
                await self.client.post("/v2/events", json=self.event(2, kind="tone"))
            ).status,
            202,
        )
        await until(lambda: self.backend.play.called)
        self.assertEqual(len(self.worker.calls), 1)
        self.worker.release.set()
        self.backend.play.assert_called_once()
        self.assertEqual(
            self.backend.play.call_args.args[0][0].data, ASSET.read_bytes()
        )

    async def test_generation_change_cancels_inference_and_waits_for_clear(
        self,
    ) -> None:
        """Do not acknowledge stop or admit new speech while sink clear is pending."""
        self.worker.release.clear()
        await self.client.post("/v2/events", json=self.event())
        await asyncio.wait_for(self.worker.started.wait(), 2)
        stopped = asyncio.Event()
        release = asyncio.Event()

        async def stop(_sink) -> None:  # noqa: ANN001
            stopped.set()
            await release.wait()

        with patch.object(SendspinPlaybackSink, "stop", stop):
            old = self.event(2)
            self.snapshot["context"].update(revision=2, generation=1)
            pending = asyncio.create_task(
                self.client.put("/v2/state", json=self.snapshot)
            )
            try:
                await asyncio.wait_for(stopped.wait(), 2)
                self.assertFalse(pending.done())
                self.assertEqual(
                    (await self.client.post("/v2/events", json=old)).status, 409
                )
            finally:
                release.set()
                self.assertEqual((await pending).status, 200)
        self.worker.release.set()
        self.backend.play.assert_not_called()
        retry = await self.client.post("/v2/events", json=old)
        self.assertEqual((await retry.json())["outcome"], "stale_context")
        self.assertEqual(
            (
                await self.client.post("/v2/events", json=self.event(3, kind="tone"))
            ).status,
            202,
        )
        await until(lambda: self.backend.play.called)

    async def test_snapshot_content_and_voice_generation_are_checked(self) -> None:
        """A display-only revision is allowed; a voice edit must invalidate audio."""
        self.snapshot["heat_name"] = "Renamed heat"
        self.assertEqual(
            (await self.client.put("/v2/state", json=self.snapshot)).status, 409
        )
        self.snapshot["context"]["revision"] = 2
        self.assertEqual(
            (await self.client.put("/v2/state", json=self.snapshot)).status, 200
        )
        self.backend.stop.assert_not_called()
        self.snapshot["context"]["revision"] = 3
        self.snapshot["voice"]["speed"] = 1.2
        self.assertEqual(
            (await self.client.put("/v2/state", json=self.snapshot)).status, 409
        )
        self.snapshot["context"]["generation"] = 1
        self.assertEqual(
            (await self.client.put("/v2/state", json=self.snapshot)).status, 200
        )
        self.backend.stop.assert_called_once_with(strict=True)

    async def test_session_retry_and_takeover_fence_old_requests(self) -> None:
        """Recover lost replies and fence delayed opens after explicit takeover."""
        retry = await self.client.post("/v2/session", json=self.open_request)
        self.assertEqual(await retry.json(), self.owner)
        takeover = {**self.owner, "epoch": "replacement", "nonce": "request-2"}
        self.assertEqual(
            (await self.client.post("/v2/session", json=takeover)).status, 409
        )
        takeover["takeover"] = True
        replacement = await (
            await self.client.post("/v2/session", json=takeover)
        ).json()
        self.assertNotEqual(replacement["session_id"], self.session_id)
        self.assertEqual(
            (await self.client.post("/v2/session", json=self.open_request)).status, 409
        )
        self.assertEqual(
            (await self.client.post("/v2/events", json=self.event())).status, 409
        )

    async def test_auth_limits_control_routes(self) -> None:
        """Protect every control route behind the configured API token."""
        for method, path in (
            ("get", "/v2/session"),
            ("post", "/v2/events"),
            ("put", "/v2/state"),
            ("post", "/v2/clock"),
            ("post", "/v2/commands"),
            ("get", "/v2/commands/unknown"),
        ):
            with self.subTest(path=path):
                response = await getattr(self.client, method)(
                    path, headers={"Authorization": "Bearer wrong"}
                )
                self.assertEqual(response.status, 401)
        self.assertEqual(
            (await self.client.post("/v2/events", data=b" " * 65_537)).status, 413
        )
        health = await (await self.client.get("/health")).json()
        self.assertNotIn("race_event_preview", health)
        self.assertNotIn("capabilities", health)

    async def test_invalid_snapshot_does_not_mutate_state(self) -> None:
        """Malformed rosters, tuning and flags fail before any generation change."""
        for key, value in (
            ("speed", True),
            ("model", "../../bad"),
            ("noise", float("nan")),
            ("enabled", 1),
            ("callout_flags", {"unknown": True}),
        ):
            with self.subTest(key=key):
                snapshot = json.loads(json.dumps(self.snapshot))
                snapshot["voice"][key] = value
                self.assertEqual(
                    (await self.client.put("/v2/state", json=snapshot)).status, 400
                )
        pilot = {"pilot_id": 1, "callsign": "Klaas", "spoken_name": "Klaas"}
        invalid = {**self.snapshot, "pilots": [pilot, pilot]}
        self.assertEqual((await self.client.put("/v2/state", json=invalid)).status, 400)
        self.assertEqual(
            (await self.client.post("/v2/events", json=self.event(kind="tone"))).status,
            202,
        )

    async def test_failed_stop_requires_retry_before_more_audio(self) -> None:
        """A backend timeout must never turn into a successful stop acknowledgement."""
        self.snapshot["context"].update(revision=2, generation=1)
        self.backend.stop.side_effect = TimeoutError("backend did not clear")
        with self.assertLogs(logger, level="ERROR"):
            response = await self.client.put("/v2/state", json=self.snapshot)
        self.assertEqual(response.status, 500)
        self.assertEqual(
            (await self.client.post("/v2/events", json=self.event())).status, 409
        )
        self.backend.stop.side_effect = None
        self.assertEqual(
            (await self.client.put("/v2/state", json=self.snapshot)).status, 200
        )
        self.assertEqual(
            (await self.client.post("/v2/events", json=self.event(kind="tone"))).status,
            202,
        )

    async def test_clock_samples_are_single_use_and_uncertainty_is_enforced(
        self,
    ) -> None:
        """Do not schedule a beep using an imprecise or replayed clock exchange."""
        probe = await (
            await self.client.post(
                "/v2/clock",
                json={"session_id": self.session_id, "sent": time.monotonic() - 1},
            )
        ).json()
        completion = {
            "session_id": self.session_id,
            "probe_id": probe["probe_id"],
            "received": time.monotonic(),
        }
        self.assertEqual(
            (await self.client.post("/v2/clock", json=completion)).status, 200
        )
        self.assertEqual(
            (await self.client.post("/v2/clock", json=completion)).status, 409
        )
        event = self.event(kind="tone")
        event["play_at"] = time.monotonic() + 1
        self.assertEqual((await self.client.post("/v2/events", json=event)).status, 409)
        await self.synchronize()
        self.assertEqual((await self.client.post("/v2/events", json=event)).status, 202)

    def command(self, sequence: int = 1, operation: str = "prepare") -> dict:
        """Capture the current acknowledged context for an explicit action."""
        return {
            "version": "race-events/1",
            "session_id": self.session_id,
            "context": dict(self.snapshot["context"]),
            "settings_revision": self.snapshot["context"]["revision"],
            "command_id": f"{self.session_id}:command:{sequence}",
            "operation": operation,
        }

    async def command_result(self, command: dict) -> dict:
        """Poll observable job completion, including cancellation or failure."""
        async with asyncio.timeout(3):
            while True:
                result = await (
                    await self.client.get(f"/v2/commands/{command['command_id']}")
                ).json()
                if result.get("status") not in ("queued", "running"):
                    return result
                await asyncio.sleep(0.001)

    async def test_manual_prepare_progress_retry_and_beep_independence(self) -> None:
        """Idle startup synthesizes nothing; explicit preparation never holds beeps."""
        self.assertEqual(self.worker.calls, [])
        self.worker.release.clear()
        command = self.command()
        self.assertEqual(
            (await self.client.post("/v2/commands", json=command)).status, 200
        )
        await asyncio.wait_for(self.worker.started.wait(), 2)
        self.assertEqual(
            (await self.client.post("/v2/commands", json=command)).status, 200
        )
        self.assertEqual(
            (await self.client.post("/v2/commands", json=self.command(2))).status, 429
        )
        self.assertEqual(
            (await self.client.post("/v2/events", json=self.event(kind="tone"))).status,
            202,
        )
        await until(lambda: self.backend.play.called)
        self.worker.release.set()
        result = await self.command_result(command)
        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["completed"], result["total"])
        self.assertEqual(result["generated"], 0)
        count = len(self.worker.calls)
        self.assertEqual(
            await (await self.client.post("/v2/commands", json=command)).json(), result
        )
        self.assertEqual(len(self.worker.calls), count)

    async def test_heat_cancels_prepare_and_only_requests_temporary_cleanup(
        self,
    ) -> None:
        """A heat change invalidates the batch without wiping reusable phrases."""
        self.worker.release.clear()
        command = self.command()
        await self.client.post("/v2/commands", json=command)
        await asyncio.wait_for(self.worker.started.wait(), 2)
        self.snapshot["context"].update(revision=2, generation=1, heat_id=2)
        self.assertEqual(
            (await self.client.put("/v2/state", json=self.snapshot)).status, 200
        )
        self.assertEqual((await self.command_result(command))["status"], "cancelled")
        await until(lambda: len(self.worker.calls) == 2)
        self.assertEqual(self.worker.calls[-1]["operation"], "clear")
        self.assertEqual(self.worker.calls[-1]["subdir"], "tmp")
        self.worker.release.set()
        stale = self.command(2)
        stale["settings_revision"] = 1
        self.assertEqual(
            (await self.client.post("/v2/commands", json=stale)).status, 409
        )

    async def test_clear_flushes_audio_and_allows_tones_while_disk_is_busy(
        self,
    ) -> None:
        """Clear pauses speech while tones remain available."""
        self.worker.release.clear()
        command = self.command(operation="clear_cache")
        await self.client.post("/v2/commands", json=command)
        await asyncio.wait_for(self.worker.started.wait(), 2)
        self.backend.stop.assert_called_once_with(strict=True)
        self.assertEqual(
            (await self.client.post("/v2/events", json=self.event())).status, 429
        )
        self.assertEqual(
            (
                await self.client.post("/v2/events", json=self.event(2, kind="tone"))
            ).status,
            202,
        )
        await until(lambda: self.backend.play.called)
        self.worker.release.set()
        result = await self.command_result(command)
        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["cleared"], 3)
        self.assertNotIn("subdir", self.worker.calls[0])

    async def test_state_change_keeps_speech_blocked_until_clear_finishes(self) -> None:
        """A new snapshot must not reopen speech while deletion is still in flight."""
        self.worker.release.clear()
        command = self.command(operation="clear_cache")
        await self.client.post("/v2/commands", json=command)
        await asyncio.wait_for(self.worker.started.wait(), 2)
        self.snapshot["context"].update(revision=2, generation=1)
        self.assertEqual(
            (await self.client.put("/v2/state", json=self.snapshot)).status, 200
        )
        self.assertEqual(
            (await self.client.post("/v2/events", json=self.event())).status, 429
        )
        self.assertEqual(
            (
                await self.client.post("/v2/events", json=self.event(2, kind="tone"))
            ).status,
            202,
        )
        await until(lambda: self.backend.play.called)
        self.worker.release.set()
        async with asyncio.timeout(2):
            sequence = 3
            while (
                await self.client.post("/v2/events", json=self.event(sequence))
            ).status == 429:
                sequence += 1
                await asyncio.sleep(0.001)
        await until(lambda: self.backend.play.call_count == 2)

    async def test_failed_cache_flush_never_deletes_or_reports_completion(self) -> None:
        """Do not delete files when the output cannot confirm that it stopped."""
        self.backend.stop.side_effect = TimeoutError("backend unavailable")
        command = self.command(operation="clear_cache")
        with self.assertLogs("sendspin_service.race.cache_commands", level="ERROR"):
            await self.client.post("/v2/commands", json=command)
            result = await self.command_result(command)
        self.assertEqual(result["status"], "failed")
        self.assertEqual(self.worker.calls, [])
        self.assertEqual(
            (await self.client.post("/v2/events", json=self.event())).status, 409
        )
        self.backend.stop.side_effect = None
        self.assertEqual(
            (await self.client.put("/v2/state", json=self.snapshot)).status, 200
        )

    async def test_takeover_cancels_prepare_and_fences_its_status(self) -> None:
        """An old publisher cannot keep generating a batch after ownership changes."""
        self.worker.release.clear()
        command = self.command()
        await self.client.post("/v2/commands", json=command)
        await asyncio.wait_for(self.worker.started.wait(), 2)
        takeover = {**self.owner, "epoch": "new", "nonce": "new", "takeover": True}
        self.assertEqual(
            (await self.client.post("/v2/session", json=takeover)).status, 200
        )
        self.assertEqual(
            (await self.client.get(f"/v2/commands/{command['command_id']}")).status, 409
        )
        self.assertEqual(
            (await self.client.post("/v2/commands", json=command)).status, 409
        )
        self.worker.release.set()

    async def test_evicted_commands_never_execute_again(self) -> None:
        """Bound history while retaining a session high-water mark for old retries."""
        first = self.command(operation="clear_cache")
        for sequence in range(1, 35):
            command = self.command(sequence, "clear_cache")
            await self.client.post("/v2/commands", json=command)
            self.assertEqual(
                (await self.command_result(command))["status"], "completed"
            )
        self.assertEqual(
            (await self.client.post("/v2/commands", json=first)).status, 410
        )
        self.assertEqual(len(self.worker.calls), 34)
        changed = {**command, "operation": "prepare"}
        self.assertEqual(
            (await self.client.post("/v2/commands", json=changed)).status, 409
        )

    async def test_scheduled_countdown_uses_service_speech_and_prepared_cache(
        self,
    ) -> None:
        """An RH snapshot schedules localized speech without a later audio event."""
        self.snapshot["context"].update(revision=2, generation=1)
        self.snapshot["scheduled_start"] = time.monotonic() + 5.4
        self.assertEqual(
            (await self.client.put("/v2/state", json=self.snapshot)).status, 200
        )
        await until(lambda: self.backend.play.called)
        self.assertEqual(len(self.worker.calls), 1)
        self.assertEqual(self.worker.calls[0]["subdir"], "precache/clock")
        self.assertEqual(self.worker.calls[0]["text"], "Next race begins in 5 seconds")
        self.assertEqual(
            (await self.client.put("/v2/state", json=self.snapshot)).status, 200
        )
        await self.synchronize()
        self.assertEqual(len(self.worker.calls), 1)

    async def test_internal_and_external_signals_share_admission_order(self) -> None:
        """Service order spans timer callbacks without consuming publisher sequences."""
        self.snapshot["context"].update(revision=2, generation=1)
        self.snapshot["scheduled_start"] = time.monotonic() + 5.6
        with patch.object(PreparationPlanner, "submit", return_value=True) as submit:
            await self.client.put("/v2/state", json=self.snapshot)
            self.assertEqual(
                (
                    await self.client.post("/v2/events", json=self.event(kind="tone"))
                ).status,
                202,
            )
            await until(lambda: submit.call_count == 2)
            self.assertEqual(
                (
                    await self.client.post(
                        "/v2/events", json=self.event(2, kind="tone")
                    )
                ).status,
                202,
            )
        plans = [call.args[0] for call in submit.call_args_list]
        self.assertEqual([p.order for p in plans], [1, 2, 3])
        self.assertEqual([p.event.sequence for p in plans], [1, 0, 2])

    async def test_schedule_drops_callout_when_clock_mapping_is_stale(self) -> None:
        """A timer cannot grant fresh lifetime after the RH clock exchange expires."""
        self.snapshot["context"].update(revision=2, generation=1)
        self.snapshot["scheduled_start"] = time.monotonic() + 5.4
        await self.client.put("/v2/state", json=self.snapshot)
        attempted = asyncio.Event()

        def stale(*_args) -> float:  # noqa: ANN002
            attempted.set()
            raise ProtocolError("Clock mapping needs refreshing")

        with patch.object(ClockMapping, "expiry", stale):
            await asyncio.wait_for(attempted.wait(), 2)
        self.assertEqual(self.worker.calls, [])
        self.backend.play.assert_not_called()

    async def test_schedule_validation_and_stop_fence_pending_countdown(self) -> None:
        """Reject malformed targets and clear pending timers on stop."""
        for value in (True, -1, "later", float("inf")):
            self.snapshot["scheduled_start"] = value
            self.assertEqual(
                (await self.client.put("/v2/state", json=self.snapshot)).status, 400
            )
        self.snapshot["scheduled_start"] = time.monotonic() + 60
        self.snapshot["context"]["revision"] = 2
        self.assertEqual(
            (await self.client.put("/v2/state", json=self.snapshot)).status, 409
        )
        self.snapshot["context"]["generation"] = 1
        self.assertEqual(
            (await self.client.put("/v2/state", json=self.snapshot)).status, 200
        )
        self.snapshot["context"].update(revision=3, generation=2)
        self.snapshot["scheduled_start"] = None
        self.assertEqual(
            (await self.client.put("/v2/state", json=self.snapshot)).status, 200
        )
        self.assertEqual(self.worker.calls, [])

    async def test_cleanup_reaps_worker(self) -> None:
        """Application shutdown owns the isolated worker lifecycle."""
        await self.client.close()
        self.assertTrue(self.worker.closed)


class FakeSink:
    """A minimal PlaybackSink test double recording calls independently of others."""

    def __init__(self, *, blocked: bool = False) -> None:
        """Default to completing playback immediately; a test can hold it open."""
        self.played: list = []
        self.stops = 0
        self.entered = asyncio.Event()
        self.release = asyncio.Event()
        if not blocked:
            self.release.set()

    async def play(self, plan, cancelled) -> None:  # noqa: ANN001, ARG002
        """Record the plan, then optionally block until the test releases it."""
        self.entered.set()
        await self.release.wait()
        self.played.append(plan)

    async def stop(self) -> None:
        """Count stop calls without touching another destination's state."""
        self.stops += 1


class RaceIngestFanOutTests(unittest.IsolatedAsyncioTestCase):
    """Prove synthesis happens once while each named destination stays independent."""

    async def asyncSetUp(self) -> None:
        """Wire two fake destinations directly, bypassing HTTP and Sendspin."""
        self.worker = Worker()
        self.sink_a = FakeSink()
        self.sink_b = FakeSink()
        self.ingest = RaceIngest(
            self.worker,
            {"a": Destination(self.sink_a), "b": Destination(self.sink_b)},
            {},
        )
        self.addAsyncCleanup(self.ingest.close)
        owner = self.ingest.owner()
        opened = await self.ingest.session(
            {**owner, "epoch": "rh-process", "nonce": "request-1"}
        )
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
            (await self.ingest.state(self.snapshot))["outcome"], "accepted"
        )
        self._synchronize_clock()

    def _synchronize_clock(self) -> None:
        """Complete the four-timestamp clock handshake without an HTTP round trip."""
        probe = self.ingest.clock(
            {"session_id": self.session_id, "sent": time.monotonic()}
        )
        self.ingest.clock(
            {
                "session_id": self.session_id,
                "probe_id": probe["probe_id"],
                "received": time.monotonic(),
            }
        )

    def _event(self, sequence: int, *, kind: str = "voice", pilot: int = 1) -> dict:
        """Build a minimal admissible event for the session under test."""
        now = time.monotonic()
        if kind == "lap":
            payload = {
                "text": "twenty seconds",
                "pilot_id": pilot,
                "pilot_name": "Klaas",
                "lap": 2,
            }
        else:
            payload = {"text": f"callout {sequence}"}
        return {
            "version": "race-events/1",
            "session_id": self.session_id,
            "event_id": f"{self.session_id}:{sequence}",
            "sequence": sequence,
            "context": dict(self.snapshot["context"]),
            "kind": kind,
            "occurred_at": now,
            "expires_at": now + 10,
            "payload": payload,
        }

    async def test_one_synthesis_serves_both_destinations(self) -> None:
        """A single voice event synthesizes once and reaches every destination."""
        result = self.ingest.event(self._event(1, kind="voice"))
        self.assertEqual(result["outcome"], "accepted")
        await until(lambda: self.sink_a.played and self.sink_b.played)
        self.assertEqual(len(self.worker.calls), 1)
        self.assertEqual(len(self.sink_a.played), 1)
        self.assertEqual(len(self.sink_b.played), 1)
        self.assertIs(self.sink_a.played[0].audio, self.sink_b.played[0].audio)

    async def test_independent_bounding_isolates_queues(self) -> None:
        """A stuck, backed-up destination cannot delay or starve another."""
        self.sink_a.release.clear()  # sink_a never finishes a play() call
        self.ingest._outputs["a"]._max_pending = 2  # noqa: SLF001

        self.assertEqual(
            self.ingest.event(self._event(1, kind="voice"))["outcome"], "accepted"
        )
        await self.sink_a.entered.wait()
        await until(lambda: len(self.sink_b.played) == 1)
        self.assertEqual(len(self.sink_a.played), 0)  # still blocked mid-play

        with self.assertLogs("sendspin_service.telemetry", level="INFO") as captured:
            for sequence in range(2, 6):
                outcome = self.ingest.event(self._event(sequence, kind="voice"))
                self.assertEqual(outcome["outcome"], "accepted")
            await until(lambda: len(self.sink_b.played) == 5)

        records = [json.loads(entry.getMessage()) for entry in captured.records]
        a_drops = [
            record
            for record in records
            if record["stage"] == "output_dropped"
            and record.get("destination") == "a"
            and record.get("reason") == "no_room"
        ]
        self.assertTrue(a_drops)
        b_drops = [
            record
            for record in records
            if record["stage"] == "output_dropped" and record.get("destination") == "b"
        ]
        self.assertEqual(b_drops, [])
        self.assertEqual(len(self.sink_b.played), 5)

    async def test_flush_and_close_reach_every_destination(self) -> None:
        """A generation change and shutdown must stop every destination, not one."""
        self.ingest.event(self._event(1, kind="voice"))
        await until(lambda: self.sink_a.played and self.sink_b.played)
        # Session/state setup already triggered a clear on both sinks; compare
        # against that baseline instead of assuming it starts at zero.
        base_a, base_b = self.sink_a.stops, self.sink_b.stops
        self.snapshot["context"].update(revision=2, generation=1)
        self.assertEqual(
            (await self.ingest.state(self.snapshot))["outcome"], "accepted"
        )
        self.assertEqual(self.sink_a.stops, base_a + 1)
        self.assertEqual(self.sink_b.stops, base_b + 1)
        await self.ingest.close()
        self.assertEqual(self.sink_a.stops, base_a + 2)
        self.assertEqual(self.sink_b.stops, base_b + 2)

    async def test_telemetry_destination_field_distinguishes_outcomes(self) -> None:
        """Two destinations' terminal records for one event_id stay attributable."""
        event_id = f"{self.session_id}:1"
        with self.assertLogs("sendspin_service.telemetry", level="INFO") as captured:
            self.ingest.event(self._event(1, kind="voice"))
            await until(lambda: self.sink_a.played and self.sink_b.played)
        records = [
            json.loads(entry.getMessage())
            for entry in captured.records
            if json.loads(entry.getMessage())["event_id"] == event_id
        ]
        scheduled = [
            record for record in records if record["stage"] == "output_scheduled"
        ]
        self.assertEqual({record["destination"] for record in scheduled}, {"a", "b"})
        self.assertEqual(len(scheduled), 2)

    async def _new_ingest(self, destinations: dict[str, Destination]) -> tuple:
        """Build, admit and clock-sync a standalone ingest for a one-off test."""
        worker = Worker()
        ingest = RaceIngest(worker, destinations, {})
        self.addAsyncCleanup(ingest.close)
        owner = ingest.owner()
        opened = await ingest.session(
            {**owner, "epoch": "rh-process", "nonce": "request-1"}
        )
        session_id = opened["session_id"]
        snapshot = {
            "version": "race-events/1",
            "session_id": session_id,
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
        self.assertEqual((await ingest.state(snapshot))["outcome"], "accepted")
        probe = ingest.clock({"session_id": session_id, "sent": time.monotonic()})
        ingest.clock(
            {
                "session_id": session_id,
                "probe_id": probe["probe_id"],
                "received": time.monotonic(),
            }
        )
        return ingest, worker, session_id, snapshot

    async def test_empty_destinations_dict_does_not_crash(self) -> None:
        """A primary with every output disabled still synthesizes without error."""
        ingest, worker, session_id, snapshot = await self._new_ingest({})
        now = time.monotonic()
        event = {
            "version": "race-events/1",
            "session_id": session_id,
            "event_id": f"{session_id}:1",
            "sequence": 1,
            "context": dict(snapshot["context"]),
            "kind": "voice",
            "occurred_at": now,
            "expires_at": now + 10,
            "payload": {"text": "hello"},
        }
        self.assertEqual(ingest.event(event)["outcome"], "accepted")
        await until(lambda: len(worker.calls) == 1)

    async def test_destination_filter_excludes_non_matching_callouts(self) -> None:
        """A destination's accepts() predicate, not just bounding, decides delivery."""
        sink_all = FakeSink()
        sink_pilot_one = FakeSink()
        ingest, _worker, session_id, snapshot = await self._new_ingest(
            {
                "all": Destination(sink_all),
                "personal": Destination(
                    sink_pilot_one, accepts=lambda plan: plan.event.pilot_id == 1
                ),
            }
        )

        def lap_event(sequence: int, pilot_id: int) -> dict:
            now = time.monotonic()
            return {
                "version": "race-events/1",
                "session_id": session_id,
                "event_id": f"{session_id}:{sequence}",
                "sequence": sequence,
                "context": dict(snapshot["context"]),
                "kind": "lap",
                "occurred_at": now,
                "expires_at": now + 10,
                "payload": {
                    "text": "twenty seconds",
                    "pilot_id": pilot_id,
                    "pilot_name": "Klaas",
                    "lap": 2,
                },
            }

        self.assertEqual(ingest.event(lap_event(1, pilot_id=1))["outcome"], "accepted")
        self.assertEqual(ingest.event(lap_event(2, pilot_id=2))["outcome"], "accepted")
        await until(lambda: len(sink_all.played) == 2)
        self.assertEqual(len(sink_pilot_one.played), 1)
        self.assertEqual(sink_pilot_one.played[0].event.pilot_id, 1)

    async def test_raising_filter_does_not_block_other_destinations(self) -> None:
        """A destination's broken predicate must not cost delivery to the rest."""

        def broken_predicate(_plan) -> bool:  # noqa: ANN001
            raise RuntimeError("boom")

        sink_ok = FakeSink()
        ingest, _worker, session_id, snapshot = await self._new_ingest(
            {
                "broken": Destination(FakeSink(), accepts=broken_predicate),
                "ok": Destination(sink_ok),
            }
        )
        now = time.monotonic()
        event = {
            "version": "race-events/1",
            "session_id": session_id,
            "event_id": f"{session_id}:1",
            "sequence": 1,
            "context": dict(snapshot["context"]),
            "kind": "voice",
            "occurred_at": now,
            "expires_at": now + 10,
            "payload": {"text": "hello"},
        }
        with self.assertLogs("sendspin_service.race.race_ingest", level="ERROR"):
            self.assertEqual(ingest.event(event)["outcome"], "accepted")
            await until(lambda: len(sink_ok.played) == 1)


class RaceModeConfigTests(unittest.TestCase):
    """Require credentials outside the local host and enable v2 by default."""

    def test_network_primary_requires_a_token(self) -> None:
        """Catch an exposed unauthenticated primary before constructing the backend."""
        with self.assertRaisesRegex(ValueError, "requires an API token"):
            SendspinService(
                ServiceConfig(api_host="0.0.0.0", race_cache_dir=Path("cache"))  # noqa: S104
            )

    def test_default_service_enables_race_routes_without_extra_config(self) -> None:
        """v2 is the only mode; a bare ServiceConfig() must register race routes."""
        self.assertEqual(ServiceConfig().race_cache_dir, DEFAULT_RACE_CACHE_DIR)
        with patch("sendspin_service.server.SendSpinServer"):
            service = SendspinService(ServiceConfig())
            app = _create_app(service)
        health = service.health()
        self.assertNotIn("race_event_preview", health)
        self.assertNotIn("supports_multipart_play", health)
        self.assertNotIn("supports_audio_references", health)
        self.assertIn(
            "/v2/events", [resource.canonical for resource in app.router.resources()]
        )
