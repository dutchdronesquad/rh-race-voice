"""Protect complete scheduled clips and client-aware playback timestamps."""

# ruff: noqa: PT009, SLF001

from __future__ import annotations

import asyncio
import threading
import unittest
from unittest.mock import AsyncMock, Mock, patch

from aiosendspin.server import AudioFormat

from sendspin_service.playback.sendspin import (
    SendSpinServer,
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
        with patch(
            "sendspin_service.playback.sendspin.time.monotonic", return_value=10.0
        ):
            self.assertEqual(
                _scheduled_play_start_us(11.0, 20_000_000, 300_000), 21_000_000
            )
            with self.assertLogs("sendspin_service.playback.sendspin", level="INFO"):
                self.assertEqual(
                    _scheduled_play_start_us(10.0, 20_000_000, 300_000), 20_300_000
                )


class CancelledStreamingTests(unittest.IsolatedAsyncioTestCase):
    """Cancellation survives time spent waiting for locks or buffer space."""

    async def test_cancel_during_buffer_wait_prevents_more_pcm(self) -> None:
        """Never commit another speech chunk after a signal cancelled it."""
        cancelled = threading.Event()
        stream = Mock(is_stopped=False)
        stream.sleep_to_limit_buffer = AsyncMock(
            side_effect=lambda **_: cancelled.set()
        )
        stream.commit_audio = AsyncMock()
        clip = _WavClip("lap", AudioFormat(24000, 16, 1), bytes(4800), 0.1)
        await _stream_wav(
            stream,
            clip,
            play_start_us=100,
            sync_clients=AsyncMock(return_value=1),
            options=_StreamOptions(max_buffer_us=500_000, cancelled=cancelled),
        )
        stream.commit_audio.assert_not_awaited()

    async def test_cancel_before_stream_lock_prevents_restarting_speech(self) -> None:
        """An obsolete job cannot create a new stream after interruption."""
        backend = SendSpinServer(advertise=False)
        backend._stream_lock = asyncio.Lock()
        backend._append_to_stream_locked = AsyncMock()
        cancelled = threading.Event()
        async with backend._stream_lock:
            task = asyncio.create_task(
                backend._append_to_stream(
                    [],
                    None,
                    None,
                    0.0,
                    1.0,
                    cancelled,
                )
            )
            cancelled.set()
        await task
        backend._append_to_stream_locked.assert_not_awaited()
