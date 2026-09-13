"""Exercise signal interruption without a physical Sendspin player."""

# ruff: noqa: PT009, SLF001

from __future__ import annotations

import asyncio
import threading
import unittest
from unittest.mock import AsyncMock, Mock

from aiosendspin.server import AudioFormat

from sendspin_service.audio_queue import AudioQueue, Priority, WavItem
from sendspin_service.sendspin import (
    SendSpinServer,
    _stream_wav,
    _StreamOptions,
    _WavClip,
)


class SignalQueueTests(unittest.TestCase):
    """Signals cancel speech but retain other signals and queued winner messages."""

    def test_signal_cancels_active_lap_and_drops_pending_laps(self) -> None:
        """Hold speech in the player while injecting two tones and a winner."""
        started = threading.Event()
        release = threading.Event()
        finished = threading.Event()
        calls = []
        tokens = []

        def player(items, *_args, cancelled):  # noqa: ANN001, ANN002, ANN202
            name = items[0].name
            calls.append(name)
            tokens.append(cancelled)
            if name == "lap":
                started.set()
                release.wait(2)
            if name == "winner":
                finished.set()

        interrupt = Mock()
        queue = AudioQueue(player, interrupt)
        try:
            queue.enqueue("lap", [WavItem("lap")], Priority.LAP)
            self.assertTrue(started.wait(1))
            queue.enqueue("stale lap", [WavItem("stale lap")], Priority.LAP)
            queue.enqueue("winner", [WavItem("winner")], Priority.HIGH)
            interrupt.assert_called_once()
            queue.enqueue("tone 1", [WavItem("tone 1")], Priority.SIGNAL)
            self.assertTrue(tokens[0].is_set())
            queue.enqueue("tone 2", [WavItem("tone 2")], Priority.SIGNAL)
            self.assertEqual(interrupt.call_count, 2)
            release.set()
            self.assertTrue(finished.wait(2))
            self.assertEqual(calls, ["lap", "tone 1", "tone 2", "winner"])
        finally:
            release.set()

    def test_ordinary_announcement_also_preempts_lap_speech(self) -> None:
        """Every announcement outranks laps, without becoming a race signal."""
        started = threading.Event()
        release = threading.Event()
        finished = threading.Event()
        calls = []
        tokens = []

        def player(items, *_args, cancelled):  # noqa: ANN001, ANN002, ANN202
            name = items[0].name
            calls.append(name)
            tokens.append(cancelled)
            if name == "lap":
                started.set()
                release.wait(2)
            else:
                finished.set()

        interrupt = Mock()
        queue = AudioQueue(player, interrupt)
        try:
            queue.enqueue("lap", [WavItem("lap")], Priority.LAP)
            self.assertTrue(started.wait(1))
            queue.enqueue("pilot done", [WavItem("pilot done")], Priority.NORMAL)
            self.assertTrue(tokens[0].is_set())
            interrupt.assert_called_once()
            release.set()
            self.assertTrue(finished.wait(2))
            self.assertEqual(calls, ["lap", "pilot done"])
        finally:
            release.set()

    def test_expired_signal_does_not_interrupt_speech(self) -> None:
        """An unusable signal must not silence a live announcement."""
        interrupt = Mock()
        queue = AudioQueue(Mock(), interrupt)
        queue.enqueue("expired", [WavItem("tone")], Priority.SIGNAL, expiry_sec=-1)
        interrupt.assert_not_called()


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
