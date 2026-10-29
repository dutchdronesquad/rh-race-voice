"""Replay actual RH phonetic events to decoded audio in an encrypted SDK client."""

# ruff: noqa: PT009, SLF001

from __future__ import annotations

import asyncio
import json
import os
import struct
import sys
import unittest
from dataclasses import replace
from itertools import pairwise
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import AsyncMock, patch

from aiohttp.test_utils import TestServer
from aiosendspin.client.client import SendspinClient
from aiosendspin.models.player import ClientHelloPlayerSupport, SupportedAudioFormat
from aiosendspin.models.types import AudioCodec, Roles
from aiosendspin.noise.keys import Identity
from aiosendspin.noise.trust_store import InMemoryClientPairingStore

from sendspin_service.playback.sendspin import SendSpinServer
from sendspin_service.server import SendspinService, ServiceConfig, _create_app
from sendspin_service.synthesis.synthesis import SynthesisWorker
from tests.test_rh_contract import ROOT, rotorhazard_source


class MarkerWorker(SynthesisWorker):
    """Keep the actual worker pipe/cache, replacing native inference with PCM."""

    def __init__(self, root: Path) -> None:
        """Assign a distinct signed-16-bit sample to every synthesized phrase."""
        super().__init__(root, worker_module="tests.helpers.synthesis_worker_probe")
        self.markers: dict[str, int] = {}

    async def _execute(self, request: bytes) -> dict:
        payload = json.loads(request)
        payload["mock_audio"] = True
        if payload.get("operation") == "synthesize":
            text = payload["text"]
            payload["pcm_marker"] = self.markers.setdefault(
                text, 1000 + len(self.markers) * 100
            )
            payload["sample_rate"] = 24000
            payload["frames"] = 2400
        return await super()._execute((json.dumps(payload) + "\n").encode())


class RotorHazardSendspinTests(unittest.IsolatedAsyncioTestCase):
    """Only host storage, native inference and listener binding are substituted."""

    async def asyncSetUp(self) -> None:
        """Start real race HTTP, output planners, SDK server and encrypted client."""
        source = rotorhazard_source()
        if source is None:
            self.skipTest("Set RACE_VOICE_RH_SOURCE; see CONTRIBUTING.md")
        root = Path(self.enterContext(TemporaryDirectory()))
        models = root / "models"
        models.mkdir()
        # The plugin's configured model is resolved through real RH option defaults.
        from custom_plugins.race_voice.const import DEFAULT_MODEL  # noqa: PLC0415

        (models / f"{DEFAULT_MODEL}.onnx").write_bytes(b"test model revision")
        (models / f"{DEFAULT_MODEL}.onnx.json").write_text("{}")
        self.worker = MarkerWorker(root)
        self.telemetry = self.enterContext(
            self.assertLogs("sendspin_service.telemetry", level="INFO")
        )
        self.backend = SendSpinServer(advertise=False, state_dir=root / "sendspin")
        with patch(
            "sendspin_service.playback.sendspin.AioSendspinServer.start_server",
            new_callable=AsyncMock,
        ):
            await self.backend._start_server()
        self.addAsyncCleanup(self.backend._close_server)
        self.backend._loop = asyncio.get_running_loop()
        self.backend._ready.set()
        listener = TestServer(self.backend._server._create_web_application())
        await listener.start_server()
        self.addAsyncCleanup(listener.close)
        store = InMemoryClientPairingStore()
        await store.store_pairing_config(
            replace(await store.get_pairing_config(), unpaired_access_enabled=True)
        )
        self.player = SendspinClient(
            Identity.generate(),
            "Race integration listener",
            [Roles.PLAYER],
            pairing_store=store,
            player_support=ClientHelloPlayerSupport(
                supported_formats=[SupportedAudioFormat(AudioCodec.PCM, 1, 24000, 16)],
                buffer_capacity=1024 * 1024,
                supported_commands=[],
            ),
            required_lead_time_ms=20,
            min_buffer_ms=20,
        )
        self.pcm = bytearray()
        self.timestamps: list[list[int]] = [[]]
        self.cleared = asyncio.Event()
        self.received = asyncio.Event()
        self.player.add_audio_chunk_listener(self.receive)
        self.player.add_stream_clear_listener(lambda _roles: self.cleared.set())
        self.player.add_stream_end_listener(lambda _roles: self.cleared.set())
        self.player.add_stream_start_listener(
            lambda _message: self.timestamps.append([])
        )
        self.addAsyncCleanup(self.player.disconnect)
        await self.player.connect(str(listener.make_url("/sendspin")))
        with (
            patch("sendspin_service.server.SendSpinServer", return_value=self.backend),
            patch(
                "sendspin_service.synthesis.synthesis.SynthesisWorker",
                return_value=self.worker,
            ),
        ):
            service = SendspinService(ServiceConfig(race_cache_dir=root))
            app = _create_app(service)
        http = TestServer(app)
        await http.start_server()
        self.addAsyncCleanup(http.close)
        self.producer = await asyncio.create_subprocess_exec(
            sys.executable,
            "-m",
            "tests.helpers.rh_contract_probe",
            str(source),
            "--publish",
            str(http.make_url("/")).rstrip("/"),
            cwd=ROOT,
            env={**os.environ, "RACE_VOICE_SERVICE_TOKEN": ""},
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        self.addAsyncCleanup(self.close_producer)
        self.assertEqual(await self.reply(), {"ready": True})

    async def close_producer(self) -> None:
        """Reap the gevent process even after an assertion or timeout."""
        self.producer.stdin.close()
        try:
            await asyncio.wait_for(self.producer.wait(), timeout=3)
        except TimeoutError:
            self.producer.kill()
            await self.producer.wait()

    async def reply(self) -> dict:
        """Fail with upstream diagnostics rather than hanging on an RH exception."""
        line = await asyncio.wait_for(self.producer.stdout.readline(), timeout=8)
        if not line:
            error = await self.producer.stderr.read()
            self.fail(error.decode())
        return json.loads(line)

    async def action(self, action: str, **payload) -> None:  # noqa: ANN003
        """Command the separate real-RH producer without blocking asyncio."""
        self.producer.stdin.write(
            (json.dumps({"action": action, **payload}) + "\n").encode()
        )
        await self.producer.stdin.drain()
        self.assertEqual(await self.reply(), {"action": action})

    def receive(
        self, timestamp: int, data: bytes, _format: object, _ahead: int
    ) -> None:
        """Capture decoded player PCM, never opening an audio device."""
        self.pcm.extend(data)
        self.timestamps[-1].append(timestamp)
        self.received.set()

    async def wait_samples(self, samples: int) -> None:
        """Use exact received sample counts as completion, not playback sleeps."""
        try:
            async with asyncio.timeout(8):
                while len(self.pcm) < samples * 2:
                    self.received.clear()
                    await self.received.wait()
        except TimeoutError:
            self.fail(
                f"Received {len(self.pcm) // 2}/{samples} samples; "
                f"phrases={self.worker.markers}; telemetry={self.telemetry.output}"
            )

    def assert_segments(self, phrases: list[str]) -> None:
        """Check every sample, catching missing, duplicate or truncated segments."""
        expected = b"".join(
            struct.pack("<h", self.worker.markers[text]) * 2400 for text in phrases
        )
        self.assertEqual(bytes(self.pcm), expected)
        for timestamps in self.timestamps:
            self.assertTrue(
                all(a < b for a, b in pairwise(timestamps)),
                timestamps,
            )

    async def test_four_pilot_burst_then_normal_finish_delivers_complete_audio(
        self,
    ) -> None:
        """All four callsigns, laps and times survive before the final winner."""
        for pilot in range(1, 5):
            await self.action("lap", pilot=pilot)
        await self.wait_samples(12 * 2400)
        phrases = [
            text
            for pilot in range(1, 5)
            for text in (f"Pilot {pilot},", "Lap 1", " 12.3")
        ]
        self.assert_segments(phrases)
        await self.action("finish")
        await self.wait_samples(13 * 2400)
        self.assert_segments([*phrases, "The winner is Pilot 1"])

    async def test_filtered_laps_stay_silent_and_abort_clears_client(self) -> None:
        """Rejected laps never synthesize; abort reaches the SDK before fresh audio."""
        await self.action("lap", pilot=1, lap=None)
        await self.action("lap", pilot=2, milliseconds=9999.9)
        await self.action("lap", pilot=3)
        await self.wait_samples(3 * 2400)
        self.assert_segments(["Pilot 3,", "Lap 1", " 12.3"])
        self.assertNotIn("Pilot 1,", self.worker.markers)
        self.assertNotIn("Pilot 2,", self.worker.markers)
        self.cleared.clear()
        await self.action("abort")
        await asyncio.wait_for(self.cleared.wait(), timeout=3)
        self.pcm.clear()
        self.timestamps = [[]]
        await self.action("lap", pilot=4)
        await self.wait_samples(3 * 2400)
        self.assert_segments(["Pilot 4,", "Lap 1", " 12.3"])
