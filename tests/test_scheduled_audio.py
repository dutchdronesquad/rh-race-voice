"""Protect complete scheduled clips and client-aware playback timestamps."""

# ruff: noqa: PT009, SLF001

from __future__ import annotations

import asyncio
import threading
import unittest
from unittest.mock import AsyncMock, Mock, patch

from aiosendspin.server import AudioFormat

from sendspin_service.playback.sendspin import (
    _SCHEDULED_BUFFER_CEILING_US,
    SendSpinServer,
    _scheduled_buffer_limit_us,
    _scheduled_play_start_us,
    _stream_wav,
    _StreamOptions,
    _WavClip,
    _with_silent_prefix,
    _with_silent_tail,
)


class ScheduledBufferLimitTests(unittest.TestCase):
    """A burst of far-scheduled clips must not balloon the client's buffer."""

    def test_far_future_target_is_capped_not_left_unbounded(self) -> None:
        """A tone scheduled seconds out must not get a seconds-deep budget.

        Queuing several such tones back to back (the whole staging sequence,
        known up front from RACE_STAGE) previously summed to multiple
        seconds of client-side buffer and triggered a client clock resync
        in practice; capping each one keeps the total bounded too.
        """
        limit = _scheduled_buffer_limit_us(
            play_start_us=8_000_000, now_us=0, duration_s=0.7
        )
        self.assertEqual(limit, _SCHEDULED_BUFFER_CEILING_US)

    def test_near_target_still_gets_its_own_duration_covered(self) -> None:
        """A tone due soon must still get enough budget to queue whole."""
        limit = _scheduled_buffer_limit_us(
            play_start_us=200_000, now_us=0, duration_s=0.7
        )
        self.assertGreaterEqual(limit, 200_000 + int(0.7 * 1_000_000))
        self.assertLessEqual(limit, _SCHEDULED_BUFFER_CEILING_US)


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

    async def test_first_chunk_of_a_scheduled_clip_is_never_throttled(self) -> None:
        """Delaying the one commit that announces a target eats its own lead.

        aiosendspin's sleep_to_limit_buffer sleeps up to 1s per call; doing
        that before the first chunk of a scheduled clip would delay the
        very commit that announces play_start_us, for no benefit -- the
        target is fixed already, so the announcement should go out as
        soon as possible regardless of how full the buffer is.
        """
        pcm = b"\x01\x02" * 2205 * 3
        clip = _WavClip("stage", AudioFormat(22050, 16, 1), pcm, 0.3)
        stream = Mock(is_stopped=False)
        stream.sleep_to_limit_buffer = AsyncMock()
        stream.commit_audio = AsyncMock(return_value=1_000_000)
        await _stream_wav(
            stream,
            clip,
            play_start_us=1_000_000,
            sync_clients=AsyncMock(return_value=1),
            options=_StreamOptions(max_buffer_us=500_000),
        )
        self.assertGreater(stream.commit_audio.call_count, 1)
        self.assertEqual(
            stream.sleep_to_limit_buffer.call_count, stream.commit_audio.call_count - 1
        )

    async def test_continuation_clip_still_throttles_every_chunk(self) -> None:
        """A clip with no explicit target (mid-stream continuation) keeps pacing."""
        pcm = b"\x01\x02" * 2205 * 3
        clip = _WavClip("voice", AudioFormat(22050, 16, 1), pcm, 0.3)
        stream = Mock(is_stopped=False)
        stream.sleep_to_limit_buffer = AsyncMock()
        stream.commit_audio = AsyncMock(return_value=1_000_000)
        await _stream_wav(
            stream,
            clip,
            play_start_us=None,
            sync_clients=AsyncMock(return_value=1),
            options=_StreamOptions(max_buffer_us=500_000),
        )
        self.assertEqual(
            stream.sleep_to_limit_buffer.call_count, stream.commit_audio.call_count
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


class SilentPrefixTests(unittest.TestCase):
    """Gaps between separately-scheduled tones must be real, sent silence."""

    def test_prefix_adds_real_silence_frames_not_a_timestamp_jump(self) -> None:
        """Prepended silence must be real PCM, not just a duration bump."""
        clip = _WavClip("stage", AudioFormat(22050, 16, 1), b"\x01\x02" * 10, 0.01)
        padded = _with_silent_prefix(clip, 0.1)
        expected_frames = int(22050 * 0.1)
        self.assertEqual(padded.pcm_data, bytes(expected_frames * 2) + clip.pcm_data)
        self.assertAlmostEqual(
            padded.duration_s, clip.duration_s + expected_frames / 22050
        )


class StreamContinuityTests(unittest.IsolatedAsyncioTestCase):
    """A scheduled clip must never start before one already queued finishes."""

    def test_close_play_at_does_not_overlap_previous_queued_clip(self) -> None:
        """A closely-timed staging tone waits for the prior one, not play_at."""
        backend = SendSpinServer(advertise=False)
        backend._next_play_start_us = 5_000_000
        with patch(
            "sendspin_service.playback.sendspin._scheduled_play_start_us",
            return_value=1_200_000,  # earlier than the still-queued clip's end
        ):
            play_start_us, _options = backend._resolve_play_start(
                1.2, 1_000_000, Mock(), 1.0, None, 0.1
            )
        self.assertEqual(play_start_us, 5_000_000)

    def test_far_future_play_at_is_used_as_is(self) -> None:
        """A play_at well after the queued clip ends is not delayed further."""
        backend = SendSpinServer(advertise=False)
        backend._next_play_start_us = 5_000_000
        with patch(
            "sendspin_service.playback.sendspin._scheduled_play_start_us",
            return_value=9_000_000,
        ):
            play_start_us, _options = backend._resolve_play_start(
                9.0, 1_000_000, Mock(), 1.0, None, 0.1
            )
        self.assertEqual(play_start_us, 9_000_000)


class GapBridgingTests(unittest.TestCase):
    """A small gap since the prior clip is bridged with real silence.

    The server's resampler silently smooths over stream jumps under
    about a second instead of honoring them, so tones queued shortly
    after the previous one played closer together than scheduled.
    """

    def test_near_gap_is_bridged_with_silence(self) -> None:
        """A sub-second gap anchors on the prior clip so silence fills it."""
        backend = SendSpinServer(advertise=False)
        backend._next_play_start_us = 5_000_000
        original_pcm = b"\x01\x02"
        clips = [_WavClip("stage", AudioFormat(22050, 16, 1), original_pcm, 0.01)]
        commit_start_us = backend._bridge_gap_with_silence(clips, 5_300_000)
        self.assertEqual(commit_start_us, 5_000_000)
        expected_frames = int(22050 * 0.3)  # 300ms gap
        self.assertEqual(clips[0].pcm_data, bytes(expected_frames * 2) + original_pcm)

    def test_wide_gap_falls_back_to_a_jump(self) -> None:
        """A gap of several seconds is not streamed as silence.

        Minutes can pass between race events sharing this stream;
        bridging that with real silence would stall every other clip
        behind it, so only gaps up to _MAX_BRIDGED_GAP_US are bridged --
        wider ones fall back to the jump, which the library does honor
        for a gap this large.
        """
        backend = SendSpinServer(advertise=False)
        backend._next_play_start_us = 5_000_000
        clips = [_WavClip("stage", AudioFormat(22050, 16, 1), b"\x01\x02", 0.01)]
        commit_start_us = backend._bridge_gap_with_silence(clips, 9_000_000)
        self.assertEqual(commit_start_us, 9_000_000)
        self.assertEqual(clips[0].pcm_data, b"\x01\x02")

    def test_no_previous_clip_is_left_alone(self) -> None:
        """The very first clip on a fresh stream has nothing to bridge from."""
        backend = SendSpinServer(advertise=False)
        backend._next_play_start_us = None
        clips = [_WavClip("stage", AudioFormat(22050, 16, 1), b"\x01\x02", 0.01)]
        commit_start_us = backend._bridge_gap_with_silence(clips, 5_000_000)
        self.assertEqual(commit_start_us, 5_000_000)
        self.assertEqual(clips[0].pcm_data, b"\x01\x02")


class CancelledStreamingTests(unittest.IsolatedAsyncioTestCase):
    """Cancellation survives time spent waiting for locks or buffer space."""

    async def test_cancel_during_buffer_wait_prevents_more_pcm(self) -> None:
        """Never commit another speech chunk after a signal cancelled it.

        sync_clients runs before the cancellation check on every chunk,
        including the (now never-throttled) first one, so it's still a
        valid injection point after the first-chunk throttle skip below.
        """
        cancelled = threading.Event()
        stream = Mock(is_stopped=False)
        stream.sleep_to_limit_buffer = AsyncMock()
        stream.commit_audio = AsyncMock()
        clip = _WavClip("lap", AudioFormat(24000, 16, 1), bytes(4800), 0.1)
        await _stream_wav(
            stream,
            clip,
            play_start_us=100,
            sync_clients=AsyncMock(side_effect=lambda: cancelled.set() or 1),
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
