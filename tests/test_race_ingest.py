"""Exercise the opt-in HTTP path through real admission and audio planning."""

# ruff: noqa: PT009, PT027, SLF001

from __future__ import annotations

import asyncio
import json
import time
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import Mock, patch

from aiohttp.test_utils import TestClient, TestServer

from sendspin_service.race_ingest import logger
from sendspin_service.race_planner import SendspinPlaybackSink
from sendspin_service.server import SendspinService, ServiceConfig, _create_app

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

    async def test_auth_limits_and_legacy_playback_exclusion(self) -> None:
        """Protect control routes and keep v1 queues out of the new audio path."""
        for method, path in (
            ("get", "/v2/session"),
            ("post", "/v2/events"),
            ("put", "/v2/state"),
            ("post", "/v2/clock"),
        ):
            with self.subTest(path=path):
                response = await getattr(self.client, method)(
                    path, headers={"Authorization": "Bearer wrong"}
                )
                self.assertEqual(response.status, 401)
        for path in ("/v1/play", "/v1/stop"):
            self.assertEqual((await self.client.post(path, json={})).status, 409)
        self.assertIsNone(self.service._queue)
        self.assertEqual(
            (await self.client.post("/v2/events", data=b" " * 65_537)).status, 413
        )
        health = await (await self.client.get("/health")).json()
        self.assertTrue(health["race_event_preview"])
        self.assertNotIn("race-events/1", health.get("capabilities", []))

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

    async def test_cleanup_reaps_worker(self) -> None:
        """Application shutdown owns the isolated worker lifecycle."""
        await self.client.close()
        self.assertTrue(self.worker.closed)


class RaceModeConfigTests(unittest.TestCase):
    """Require explicit activation and credentials outside the local host."""

    def test_network_primary_requires_a_token(self) -> None:
        """Catch an exposed unauthenticated primary before constructing the backend."""
        with self.assertRaisesRegex(ValueError, "requires an API token"):
            SendspinService(
                ServiceConfig(api_host="0.0.0.0", race_cache_dir=Path("cache"))  # noqa: S104
            )

    def test_default_service_keeps_legacy_mode(self) -> None:
        """Do not register or load primary routes in ordinary playback deployments."""
        with (
            patch("sendspin_service.server.SendSpinServer"),
            patch("sendspin_service.server.AudioQueue"),
        ):
            service = SendspinService(ServiceConfig())
            app = _create_app(service)
        self.assertFalse(service.health()["race_event_preview"])
        self.assertTrue(service.health()["supports_audio_references"])
        self.assertNotIn(
            "/v2/events", [resource.canonical for resource in app.router.resources()]
        )
