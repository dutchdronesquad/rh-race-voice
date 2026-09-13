"""Protect complete scheduled clips and client-aware playback timestamps."""

# ruff: noqa: PT009

from __future__ import annotations

import unittest
from unittest.mock import AsyncMock, Mock, patch

from aiosendspin.server import AudioFormat

from sendspin_service.sendspin import (
    _scheduled_play_start_us,
    _stream_wav,
    _StreamOptions,
    _WavClip,
    _with_silent_tail,
)


class ScheduledAudioTests(unittest.IsolatedAsyncioTestCase):
    """Verify PCM delivery without requiring a Windows audio device."""

    async def test_tone_and_silent_tail_are_sent_in_bounded_chunks(self) -> None:
        """Preserve every original sample and feed trailing buffered samples."""
        pcm = b"\x01\x02" * 2205
        clip = _WavClip("tone", AudioFormat(22050, 16, 1), pcm, 0.1)
        padded = _with_silent_tail(clip)
        stream = Mock(is_stopped=False)
        stream.sleep_to_limit_buffer = AsyncMock()
        stream.commit_audio = AsyncMock(return_value=1_000_000)
        await _stream_wav(
            stream,
            padded,
            play_start_us=1_000_000,
            sync_clients=AsyncMock(return_value=1),
            options=_StreamOptions(max_buffer_us=500_000),
        )
        chunks = [c.args[0] for c in stream.prepare_audio.call_args_list]
        self.assertEqual(b"".join(chunks), pcm + bytes(len(pcm)))
        self.assertTrue(all(len(chunk) <= 2204 for chunk in chunks))
        self.assertEqual(
            stream.commit_audio.call_args_list[0].kwargs, {"play_start_us": 1_000_000}
        )
        self.assertTrue(
            all(
                c.kwargs["play_start_us"] is None
                for c in stream.commit_audio.call_args_list[1:]
            )
        )

    def test_future_target_is_preserved_and_late_target_uses_client_lead(self) -> None:
        """Do not force future targets later; disclose unavoidable late starts."""
        with patch("sendspin_service.sendspin.time.monotonic", return_value=10.0):
            self.assertEqual(
                _scheduled_play_start_us(11.0, 20_000_000, 300_000), 21_000_000
            )
            with self.assertLogs("sendspin_service.sendspin", level="INFO"):
                self.assertEqual(
                    _scheduled_play_start_us(10.0, 20_000_000, 300_000), 20_300_000
                )
