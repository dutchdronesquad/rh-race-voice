"""Exercise the first relay listener arriving after audio has already started."""

# ruff: noqa: PT009, SLF001

from __future__ import annotations

import asyncio
import io
import threading
import time
import unittest
import wave
from dataclasses import replace
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import AsyncMock, patch

from aiohttp.test_utils import TestServer
from aiosendspin.client.client import SendspinClient
from aiosendspin.models.core import DeviceInfo
from aiosendspin.models.player import ClientHelloPlayerSupport, SupportedAudioFormat
from aiosendspin.models.types import AudioCodec, GoodbyeReason, Roles
from aiosendspin.noise.keys import Identity
from aiosendspin.noise.trust_store import InMemoryClientPairingStore
from aiosendspin.server import AudioFormat

from sendspin_service.playback.audio_queue import WavItem
from sendspin_service.playback.sendspin import (
    SendSpinServer,
    _skip_elapsed_audio,
    _WavClip,
)


def _wav(duration_s: float) -> WavItem:
    """Encode numbered tenths of a second so replaying the beginning is visible."""
    data = io.BytesIO()
    with wave.open(data, "wb") as wav:
        wav.setparams((1, 2, 22050, 0, "NONE", "not compressed"))
        for index in range(round(duration_s * 10)):
            wav.writeframes((index + 1).to_bytes(2, "little") * 2205)
    return WavItem(name="programme.wav", data=data.getvalue())


class FirstListenerTests(unittest.IsolatedAsyncioTestCase):
    """Use actual encrypted SDK connections and the public synchronous play API."""

    async def asyncSetUp(self) -> None:
        """Bind an ephemeral test listener without mDNS or production clients."""
        root = Path(self.enterContext(TemporaryDirectory()))
        self.backend = SendSpinServer(advertise=False, state_dir=root)
        with patch(
            "sendspin_service.playback.sendspin.AioSendspinServer.start_server",
            new_callable=AsyncMock,
        ):
            await self.backend._start_server()
        self.http = TestServer(self.backend._server._create_web_application())
        await self.http.start_server()
        self.addAsyncCleanup(self.http.close)
        self.addAsyncCleanup(self.backend._close_server)
        self.backend._loop = asyncio.get_running_loop()
        self.backend._ready.set()
        self.audio: list[bytes] = []
        self.timestamps: list[int] = []
        self.received = asyncio.Event()

    async def connect(
        self, *, unpaired: bool = True, device_info: DeviceInfo | None = None
    ) -> SendspinClient:
        """Connect the relay's first player and capture decoded PCM."""
        store = InMemoryClientPairingStore()
        await store.store_pairing_config(
            replace(await store.get_pairing_config(), unpaired_access_enabled=unpaired)
        )
        client = SendspinClient(
            Identity.generate(),
            "Late relay listener",
            [Roles.PLAYER],
            pairing_store=store,
            device_info=device_info,
            player_support=ClientHelloPlayerSupport(
                supported_formats=[SupportedAudioFormat(AudioCodec.PCM, 1, 22050, 16)],
                buffer_capacity=1024 * 1024,
                supported_commands=[],
            ),
            required_lead_time_ms=20,
            min_buffer_ms=20,
        )

        def receive(
            _timestamp: int, data: bytes, _format: object, _send_ahead: int
        ) -> None:
            self.audio.append(data)
            self.timestamps.append(_timestamp)
            self.received.set()

        client.add_audio_chunk_listener(receive)
        self.addAsyncCleanup(client.disconnect)
        await client.connect(str(self.http.make_url("/sendspin")))
        return client

    async def test_first_listener_joins_current_position_after_admission_deadline(
        self,
    ) -> None:
        """An admitted long track keeps advancing beyond its event's start deadline."""
        playback = asyncio.create_task(
            asyncio.to_thread(
                self.backend.play, [_wav(1.2)], expires_at=time.monotonic() + 0.35
            )
        )
        await asyncio.sleep(0.45)
        await self.connect()
        await asyncio.wait_for(self.received.wait(), timeout=2)
        self.assertGreaterEqual(int.from_bytes(self.audio[0][:2], "little"), 4)
        self.assertTrue(await asyncio.wait_for(playback, timeout=2))

    async def test_no_listener_before_end_does_not_replay_old_audio(self) -> None:
        """A connection after the programme ended must stay silent."""
        self.assertFalse(await asyncio.to_thread(self.backend.play, [_wav(0.1)]))
        await self.connect()
        await asyncio.sleep(0.15)
        self.assertEqual(self.audio, [])
        self.assertIsNone(self.backend._stream)

    async def test_only_listener_reconnects_during_programme(self) -> None:
        """Explicitly disconnecting the last player must not discard the track."""
        client = await self.connect()
        playback = asyncio.create_task(
            asyncio.to_thread(self.backend.play, [_wav(2.5)])
        )
        await asyncio.wait_for(self.received.wait(), timeout=2)
        await client.disconnect(GoodbyeReason.USER_REQUEST)
        await asyncio.sleep(0.3)
        self.audio.clear()
        self.received.clear()
        await client.connect(str(self.http.make_url("/sendspin")))
        await asyncio.wait_for(self.received.wait(), timeout=2)
        self.assertGreaterEqual(int.from_bytes(self.audio[0][:2], "little"), 4)
        self.assertTrue(await asyncio.wait_for(playback, timeout=3))

    async def test_new_identity_joins_after_last_listener_leaves(self) -> None:
        """Reloading a tab can create a different SDK client and group."""
        client = await self.connect()
        playback = asyncio.create_task(asyncio.to_thread(self.backend.play, [_wav(2)]))
        await asyncio.wait_for(self.received.wait(), timeout=2)
        await client.disconnect()
        await asyncio.sleep(0.3)
        self.audio.clear()
        self.received.clear()
        await self.connect()
        await asyncio.wait_for(self.received.wait(), timeout=2)
        self.assertGreaterEqual(int.from_bytes(self.audio[0][:2], "little"), 4)
        self.assertTrue(await asyncio.wait_for(playback, timeout=3))

    async def test_reconnect_recovers_already_buffered_audio(self) -> None:
        """Short clips also survive after play() finished queueing their PCM."""
        client = await self.connect()
        target = time.monotonic() + 1
        self.assertTrue(
            await asyncio.to_thread(self.backend.play, [_wav(0.8)], play_at=target)
        )
        start_us = self.backend._retained_audio[0].start_us
        await client.disconnect()
        await asyncio.sleep(0.1)
        self.audio.clear()
        self.timestamps.clear()
        self.received.clear()
        await client.connect(str(self.http.make_url("/sendspin")))
        await asyncio.wait_for(self.received.wait(), timeout=1)
        expected_sample = max(1, (self.timestamps[0] - start_us) // 100_000 + 1)
        self.assertLessEqual(
            abs(int.from_bytes(self.audio[0][:2], "little") - expected_sample), 1
        )

    async def test_repeated_reconnect_keeps_original_position(self) -> None:
        """Multiple dropouts cannot reset the retained track's logical start."""
        client = await self.connect()
        playback = asyncio.create_task(asyncio.to_thread(self.backend.play, [_wav(3)]))
        await asyncio.wait_for(self.received.wait(), timeout=2)
        previous = 0
        for _ in range(2):
            await client.disconnect()
            await asyncio.sleep(0.25)
            self.audio.clear()
            self.received.clear()
            await client.connect(str(self.http.make_url("/sendspin")))
            await asyncio.wait_for(self.received.wait(), timeout=2)
            sample = int.from_bytes(self.audio[0][:2], "little")
            self.assertGreater(sample, previous)
            previous = sample
        self.assertTrue(await asyncio.wait_for(playback, timeout=3))

    async def test_reconnect_after_programme_end_stays_silent(self) -> None:
        """Retaining a stream must not make a completed test sound restart."""
        client = await self.connect()
        self.assertTrue(await asyncio.to_thread(self.backend.play, [_wav(0.3)]))
        await client.disconnect()
        await asyncio.sleep(0.7)
        self.audio.clear()
        self.received.clear()
        await client.connect(str(self.http.make_url("/sendspin")))
        await asyncio.sleep(0.2)
        self.assertEqual(self.audio, [])

    async def test_stop_while_disconnected_discards_retained_audio(self) -> None:
        """Stop/context changes must invalidate audio before a player comes back."""
        client = await self.connect()
        playback = asyncio.create_task(asyncio.to_thread(self.backend.play, [_wav(2)]))
        await asyncio.wait_for(self.received.wait(), timeout=2)
        await client.disconnect()
        await asyncio.wait_for(playback, timeout=1)
        await self.backend._stop_stream()
        self.audio.clear()
        self.received.clear()
        await client.connect(str(self.http.make_url("/sendspin")))
        await asyncio.sleep(0.2)
        self.assertEqual(self.audio, [])

    async def test_cancel_while_disconnected_discards_retained_audio(self) -> None:
        """Planner cancellation applies to buffered audio kept for reconnect too."""
        client = await self.connect()
        cancelled = threading.Event()
        self.assertTrue(
            await asyncio.to_thread(self.backend.play, [_wav(0.8)], cancelled=cancelled)
        )
        await client.disconnect()
        cancelled.set()
        self.audio.clear()
        self.received.clear()
        await client.connect(str(self.http.make_url("/sendspin")))
        await asyncio.sleep(0.2)
        self.assertEqual(self.audio, [])

    async def test_reconnect_preserves_future_scheduled_target(self) -> None:
        """Recreating transport cannot bring a staging tone forward in time."""
        client = await self.connect()
        target = time.monotonic() + 0.8
        expected_us = self.backend._server.clock.now_us() + round(
            (target - time.monotonic()) * 1_000_000
        )
        self.assertTrue(
            await asyncio.to_thread(self.backend.play, [_wav(0.3)], play_at=target)
        )
        await client.disconnect()
        await asyncio.sleep(0.1)
        self.audio.clear()
        self.timestamps.clear()
        self.received.clear()
        await client.connect(str(self.http.make_url("/sendspin")))
        await asyncio.wait_for(self.received.wait(), timeout=1)
        self.assertEqual(int.from_bytes(self.audio[0][:2], "little"), 1)
        self.assertLess(abs(self.timestamps[0] - expected_us), 50_000)

    async def test_expired_audio_is_not_retained(self) -> None:
        """An event already too old to start must not wait for a listener."""
        result = await asyncio.wait_for(
            asyncio.to_thread(
                self.backend.play, [_wav(2)], expires_at=time.monotonic() - 1
            ),
            timeout=1,
        )
        self.assertFalse(result)
        self.assertIsNone(self.backend._stream)

    async def test_listener_before_scheduled_target_hears_whole_clip(self) -> None:
        """Waiting for a listener must preserve a future staging-tone target."""
        target = time.monotonic() + 0.8
        expected_us = self.backend._server.clock.now_us() + round(
            (target - time.monotonic()) * 1_000_000
        )
        playback = asyncio.create_task(
            asyncio.to_thread(
                self.backend.play,
                [_wav(0.3)],
                play_at=target,
                expires_at=target + 0.5,
            )
        )
        await asyncio.sleep(0.05)
        await self.connect()
        await asyncio.wait_for(self.received.wait(), timeout=2)
        self.assertEqual(int.from_bytes(self.audio[0][:2], "little"), 1)
        self.assertLess(abs(self.timestamps[0] - expected_us), 50_000)
        self.assertTrue(await asyncio.wait_for(playback, timeout=2))

    async def test_stop_cancels_unattended_audio(self) -> None:
        """Stop acknowledges and prevents a subsequent listener hearing old audio."""
        playback = asyncio.create_task(asyncio.to_thread(self.backend.play, [_wav(2)]))
        await asyncio.sleep(0.05)
        await self.backend._stop_stream()
        self.assertFalse(await asyncio.wait_for(playback, timeout=1))
        await self.connect()
        await asyncio.sleep(0.15)
        self.assertEqual(self.audio, [])

    async def test_cancel_cancels_unattended_audio(self) -> None:
        """Planner invalidation must release playback waiting for a first listener."""
        cancelled = threading.Event()
        playback = asyncio.create_task(
            asyncio.to_thread(self.backend.play, [_wav(2)], cancelled=cancelled)
        )
        await asyncio.sleep(0.05)
        cancelled.set()
        self.assertFalse(await asyncio.wait_for(playback, timeout=1))
        self.assertIsNone(self.backend._stream)

    async def test_client_disabling_unpaired_playback_has_no_audio_role(self) -> None:
        """Windows 3 defaults cannot be overridden by server-side approval alone."""
        with self.assertLogs(
            "sendspin_service.playback.sendspin", level="WARNING"
        ) as logs:
            client = await self.connect(unpaired=False)
            await asyncio.sleep(0.05)
        self.assertIn("disables unpaired playback", " ".join(logs.output))
        server_client = self.backend._server.get_client(client.identity.peer_id)
        self.assertEqual(server_client.active_roles, ())
        self.assertTrue(await asyncio.to_thread(self.backend.play, [_wav(0.1)]))
        self.assertEqual(self.audio, [])

    async def test_newer_windows_client_can_receive_audio_over_real_transport(
        self,
    ) -> None:
        """Allow newer Windows versions when the client permits playback."""
        await self.connect(
            device_info=DeviceInfo(
                product_name="Sendspin for Windows", software_version="3.0.0"
            )
        )
        self.assertEqual(self.backend.connected_client_count(), 1)
        self.assertTrue(await asyncio.to_thread(self.backend.play, [_wav(0.1)]))
        await asyncio.wait_for(self.received.wait(), timeout=1)
        self.assertTrue(self.audio)


class AudioPositionTests(unittest.TestCase):
    """Preserve sample boundaries and cross-clip programme positions."""

    def test_skip_crosses_clip_boundary_without_splitting_stereo_frames(self) -> None:
        """Join in the second segment and retain all later segments unchanged."""
        audio_format = AudioFormat(44100, 16, 2)
        clips = [
            _WavClip(str(index), audio_format, bytes([index]) * 44100 * 4, 1.0)
            for index in range(3)
        ]
        result = _skip_elapsed_audio(clips, 1_500_001)
        self.assertEqual(len(result), 2)
        self.assertEqual(result[0].pcm_data, bytes([1]) * 22050 * 4)
        self.assertEqual(result[0].duration_s, 0.5)
        self.assertIs(result[1], clips[2])
        self.assertEqual(_skip_elapsed_audio(clips, 3_000_000), [])
