"""Probe independent groups using the installed SDK and captured PCM delivery."""

# ruff: noqa: PT009, SLF001

from __future__ import annotations

import time
import unittest
from itertools import pairwise
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, Mock, PropertyMock, patch

from aiosendspin.models.core import ClientHelloPayload
from aiosendspin.models.player import ClientHelloPlayerSupport, SupportedAudioFormat
from aiosendspin.models.types import AudioCodec
from aiosendspin.server import AudioFormat
from aiosendspin.server.push_stream import MAIN_CHANNEL, PushStream
from aiosendspin.server.roles import AudioRequirements

from sendspin_service.playback.sendspin import SendSpinServer, _stream_wav, _WavClip

if TYPE_CHECKING:
    from aiosendspin.server.client import SendspinClient


class IndependentGroupTests(unittest.IsolatedAsyncioTestCase):
    """Use real groups/streams, replacing only transport/audio-device callbacks."""

    async def asyncSetUp(self) -> None:
        """Construct the SDK without binding ports or discovering players."""
        root = Path(self.enterContext(TemporaryDirectory()))
        self.backend = SendSpinServer(advertise=False, state_dir=root)
        with patch(
            "sendspin_service.playback.sendspin.AioSendspinServer.start_server",
            new_callable=AsyncMock,
        ):
            await self.backend._start_server()
        self.addAsyncCleanup(self.backend._close_server)
        self.server = self.backend._server
        self.captures: dict[str, Mock] = {}

    def client(self, name: str) -> SendspinClient:
        """Create a real player role with a controlled preconnect PCM sink."""
        hello = ClientHelloPayload(
            name=name,
            client_id=name,
            supported_roles=["player@v1"],
            player_support=ClientHelloPlayerSupport(
                supported_formats=[SupportedAudioFormat(AudioCodec.PCM, 1, 22050, 16)],
                buffer_capacity=1024 * 1024,
                supported_commands=[],
            ),
        )
        client = self.server.get_or_create_client(name)
        client.preinitialize_client_from_hello(hello)
        role = client.role("player@v1")
        for method, value in (
            ("supports_preconnect_audio", True),
            ("get_audio_requirements", AudioRequirements(22050, 16, 1)),
        ):
            self.enterContext(patch.object(role, method, return_value=value))
        self.enterContext(patch.object(role, "on_stream_start"))
        self.captures[name] = self.enterContext(patch.object(role, "on_audio_chunk"))
        return client

    async def send(self, stream: PushStream, sample: bytes) -> None:
        """Commit recognizable PCM without simulating the SDK scheduler itself."""
        stream.prepare_audio(
            sample * 2205, AudioFormat(22050, 16, 1), channel_id=MAIN_CHANNEL
        )
        await stream.commit_audio()

    def samples(self, name: str) -> bytes:
        """Collect delivered PCM for assertions about routing and isolation."""
        return b"".join(c.args[0].data for c in self.captures[name].call_args_list)

    async def test_two_groups_receive_distinct_pcm_and_stop_independently(self) -> None:
        """Stopping a personal selection must not stop the venue programme."""
        venue = self.client("venue")
        personal = self.client("personal")
        venue_stream = venue.group.start_stream()
        personal_stream = personal.group.start_stream()
        await self.send(venue_stream, b"\x01\x00")
        await self.send(personal_stream, b"\x02\x00")
        self.assertEqual(self.samples("venue"), b"\x01\x00" * 2205)
        self.assertEqual(self.samples("personal"), b"\x02\x00" * 2205)
        await personal.group.stop()
        self.assertTrue(personal_stream.is_stopped)
        self.assertFalse(venue_stream.is_stopped)
        await self.send(venue_stream, b"\x03\x00")
        self.assertTrue(self.samples("venue").endswith(b"\x03\x00" * 2205))

    async def test_moving_listener_leaves_other_members_stream_alive(self) -> None:
        """A selection switch must preserve a shared group's remaining listeners."""
        venue = self.client("venue")
        listener = self.client("listener")
        coach = self.client("coach")
        await venue.group.add_client(listener)
        stream = venue.group.start_stream()
        await self.send(stream, b"\x01\x00")
        old_group = venue.group
        await coach.group.add_client(listener)
        self.assertIs(venue.group, old_group)
        self.assertIs(listener.group, coach.group)
        self.assertFalse(stream.is_stopped)
        before = self.samples("listener")
        await self.send(stream, b"\x04\x00")
        self.assertEqual(self.samples("listener"), before)
        self.assertTrue(self.samples("venue").endswith(b"\x04\x00" * 2205))

    async def test_scheduled_tone_keeps_contiguous_chunks_for_high_lead_player(
        self,
    ) -> None:
        """Buffer throttling must not make the SDK rebase inside a start buzzer."""
        await self.assert_contiguous_audio(
            scheduled=True, lead_us=1_600_000, min_buffer_us=1_600_000
        )

    async def test_normal_audio_respects_minimum_buffer_above_startup_lead(
        self,
    ) -> None:
        """Speech needs the same protection even when required startup lead is low."""
        await self.assert_contiguous_audio(
            scheduled=False, lead_us=100_000, min_buffer_us=750_000
        )

    async def assert_contiguous_audio(
        self, *, scheduled: bool, lead_us: int, min_buffer_us: int
    ) -> None:
        """Use real SDK pacing/encoding and controlled negotiated role metadata."""
        client = self.client("high-lead")
        role = client.role("player@v1")
        with (
            patch.object(
                type(client),
                "active_roles",
                new_callable=PropertyMock,
                return_value=(role,),
            ),
            patch.object(
                client.group.group_role("player"),
                "get_player_clients",
                return_value=[client],
            ),
            patch.object(role, "get_required_lead_time_us", return_value=lead_us),
            patch.object(role, "get_min_buffer_us", return_value=min_buffer_us),
            patch.object(role, "get_output_delay_us", return_value=0),
        ):
            stream = client.group.start_stream()
            start_us, options = self.backend._resolve_play_start(
                time.monotonic() + 1.7 if scheduled else None,
                self.server.clock.now_us(),
                client.group,
                1.0,
                None,
                0.8,
            )
            clip = _WavClip(
                "buzzer", AudioFormat(22050, 16, 1), b"\x01\x00" * 17640, 0.8
            )
            await _stream_wav(
                stream,
                clip,
                play_start_us=start_us,
                sync_clients=AsyncMock(return_value=1),
                options=options,
            )
        chunks = [call.args[0] for call in self.captures["high-lead"].call_args_list]
        self.assertGreater(len(chunks), 2)
        self.assertEqual(self.samples("high-lead"), clip.pcm_data)
        gaps = [
            following.timestamp_us - previous.timestamp_us - previous.duration_us
            for previous, following in pairwise(chunks)
        ]
        self.assertLessEqual(max(abs(gap) for gap in gaps), 1)


if __name__ == "__main__":
    unittest.main()
