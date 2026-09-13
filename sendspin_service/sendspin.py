"""Sendspin server adapter for streaming WAV audio over the local network."""

from __future__ import annotations

import asyncio
import contextlib
import io
import logging
import os
import threading
import time
import wave
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Protocol

import numpy as np
from aiosendspin.noise.keys import Identity
from aiosendspin.noise.trust_store import FileServerPairingStore, PskCategory
from aiosendspin.server import AudioFormat, ClientConnectedEvent
from aiosendspin.server import SendspinServer as AioSendspinServer
from aiosendspin.server.push_stream import MAIN_CHANNEL, StreamStoppedError

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from aiosendspin.server.group import SendspinGroup
    from aiosendspin.server.server import SendspinEvent

    from .audio_queue import WavItem

logger = logging.getLogger(__name__)

_CHUNK_DURATION_S = 0.05
_INITIAL_PLAYBACK_DELAY_S = 0.25
_TIMEOUT_MARGIN_S = 10
_BUFFER_LIMIT_US = 500_000
_LATE_JOIN_SYNC_INTERVAL_S = 0.1
_MIN_SCHEDULE_DELAY_S = 0.05
_SCHEDULED_BUFFER_MARGIN_US = 100_000
_STARTUP_TIMEOUT_S = 30.0


@dataclass(frozen=True)
class _StreamOptions:
    max_buffer_us: int
    full_clip: bool = False
    volume: float = 1.0


@dataclass(frozen=True)
class _WavClip:
    name: str
    audio_format: AudioFormat
    pcm_data: bytes
    duration_s: float


class _SendspinClock(Protocol):
    def now_us(self) -> int:
        """Return the current Sendspin monotonic clock in microseconds."""
        ...


class _PushStream(Protocol):
    @property
    def is_stopped(self) -> bool:
        """Whether the stream has been stopped."""
        ...

    async def sleep_to_limit_buffer(self, max_buffer_us: int) -> None:
        """Throttle when the queued client buffer is above the requested size."""
        ...

    def prepare_audio(
        self, pcm: bytes, audio_format: AudioFormat, *, channel_id: object
    ) -> None:
        """Prepare PCM audio for the next commit."""
        ...

    async def commit_audio(self, *, play_start_us: int | None = None) -> int:
        """Commit prepared audio and return the chunk start timestamp."""
        ...

    def clear(self) -> None:
        """Clear pending and buffered client audio."""
        ...

    def stop(self, *, keep_stream: bool = False) -> None:
        """Stop the stream transport."""
        ...


class SendSpinServer:
    """Thin synchronous adapter around ``aiosendspin``.

    RotorHazard plugin callbacks are synchronous, while aiosendspin is asyncio
    native. This class owns a background event loop and exposes blocking
    ``play()`` / ``stop()`` methods for the existing ``AudioQueue`` worker.
    Normal ``play()`` calls append to the active stream instead of stopping it,
    so queued lap callouts can be scheduled back-to-back without audible resets.
    """

    def __init__(
        self,
        host: str = "0.0.0.0",  # noqa: S104
        port: int = 8927,
        *,
        advertise: bool = True,
        state_dir: Path | None = None,
    ) -> None:
        """Configure host and port; call ``start()`` to launch the server."""
        self._host = host
        self._port = port
        self._advertise = advertise
        self._state_dir = state_dir or Path(
            os.environ.get("SENDSPIN_STATE_DIR")
            or os.environ.get("STATE_DIRECTORY")
            or Path.home() / ".local/share/sendspin-service"
        )
        self._client_tasks: set[asyncio.Task[None]] = set()
        self._loop: asyncio.AbstractEventLoop | None = None
        self._server: AioSendspinServer | None = None
        self._ready = threading.Event()
        self._startup_error: BaseException | None = None
        self._thread: threading.Thread | None = None
        self._stream_lock: asyncio.Lock | None = None
        self._stream_group: SendspinGroup | None = None
        self._stream: _PushStream | None = None
        self._next_play_start_us: int | None = None
        self._idle_stop_task: asyncio.Task[None] | None = None

    def start(self) -> None:
        """Start the Sendspin server in a background daemon thread."""
        if self._thread is not None and self._thread.is_alive():
            if self._server is None:
                message = "Sendspin server thread is running but not ready"
                raise RuntimeError(message)
            return
        self._ready.clear()
        self._startup_error = None
        self._thread = threading.Thread(
            target=self._run_loop, daemon=True, name="sendspin-service"
        )
        self._thread.start()
        if not self._ready.wait(timeout=_STARTUP_TIMEOUT_S):
            message = (
                "Sendspin server did not finish startup within "
                f"{_STARTUP_TIMEOUT_S:.0f}s"
            )
            raise RuntimeError(message)
        if self._startup_error is not None:
            message = "Sendspin server failed to start"
            raise RuntimeError(message) from self._startup_error
        if self._server is None:
            message = "Sendspin server did not become ready"
            raise RuntimeError(message)

    def play(
        self,
        wav_items: list[WavItem],
        expires_at: float | None = None,
        play_at: float | None = None,
        volume: float = 1.0,
    ) -> None:
        """Queue WAV files to connected clients without resetting active playback."""
        if not self._ready.wait(timeout=5.0) or self._loop is None:
            logger.warning("Sendspin service: Sendspin server not yet ready")
            return
        if self._server is None:
            logger.warning("Sendspin service: Sendspin server failed to start")
            return
        if not self._server.connected_clients:
            logger.info(
                "Sendspin service: no Sendspin clients connected - audio dropped"
            )
            return

        clips = _read_wav_clips(wav_items)
        if not clips:
            logger.warning("Sendspin service: no readable WAV files to play")
            return

        duration_s = sum(clip.duration_s for clip in clips)
        timeout = max(30.0, duration_s + _INITIAL_PLAYBACK_DELAY_S + _TIMEOUT_MARGIN_S)
        future = asyncio.run_coroutine_threadsafe(
            self._append_to_stream(clips, expires_at, play_at, duration_s, volume),
            self._loop,
        )
        try:
            future.result(timeout=timeout)
        except TimeoutError:
            logger.warning("Sendspin service: Sendspin stream timed out")
        except Exception:
            logger.exception("Sendspin service: Sendspin stream error")

    def stop(self) -> None:
        """Stop current playback and clear scheduled client audio."""
        if self._loop is None or self._server is None:
            return
        future = asyncio.run_coroutine_threadsafe(self._stop_stream(), self._loop)
        try:
            future.result(timeout=5.0)
        except TimeoutError:
            logger.warning("Sendspin service: Sendspin stop timed out")
        except Exception:
            logger.exception("Sendspin service: Sendspin stop error")

    def close(self) -> None:
        """Close client connections, stop the event loop, and join the thread."""
        loop = self._loop
        thread = self._thread
        if loop is None:
            return

        if loop.is_running():
            future = asyncio.run_coroutine_threadsafe(self._close_server(), loop)
            try:
                future.result(timeout=5.0)
            except TimeoutError:
                logger.warning("Sendspin service: Sendspin close timed out")
            except Exception:
                logger.exception("Sendspin service: Sendspin close error")
            loop.call_soon_threadsafe(loop.stop)

        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=5.0)
            if thread.is_alive():
                logger.warning("Sendspin service: Sendspin thread did not stop cleanly")

    def connected_client_count(self) -> int:
        """Return the number of currently connected Sendspin clients."""
        server = self._server
        if server is None:
            return 0
        return len(server.connected_clients)

    def _run_loop(self) -> None:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        loop.set_exception_handler(_handle_loop_exception)
        self._loop = loop
        try:
            try:
                loop.run_until_complete(self._start_server())
            except Exception as exc:
                self._startup_error = exc
                logger.exception("Sendspin service: Sendspin server startup failed")
                return
            self._ready.set()
            try:
                loop.run_forever()
            except Exception:
                logger.exception("Sendspin service: Sendspin server loop failed")
        finally:
            self._ready.set()
            with contextlib.suppress(Exception):
                loop.run_until_complete(self._close_server())
            self._drain_pending_tasks(loop)
            loop.close()

    @staticmethod
    def _drain_pending_tasks(loop: asyncio.AbstractEventLoop) -> None:
        pending = [task for task in asyncio.all_tasks(loop) if not task.done()]
        for task in pending:
            task.cancel()
        if pending:
            with contextlib.suppress(Exception):
                loop.run_until_complete(
                    asyncio.gather(*pending, return_exceptions=True)
                )
        with contextlib.suppress(Exception):
            loop.run_until_complete(loop.shutdown_asyncgens())

    async def _start_server(self) -> None:
        identity = await asyncio.to_thread(_load_identity, self._state_dir)
        pairing_store = await FileServerPairingStore.open(
            self._state_dir / "pairings.json"
        )
        server = AioSendspinServer(
            loop=asyncio.get_running_loop(),
            identity=identity,
            server_name="Sendspin Service",
            pairing_store=pairing_store,
            allow_unencrypted=True,
        )
        server.add_event_listener(self._on_client_event)
        try:
            await server.start_server(
                port=self._port,
                host=self._host,
                advertise_addresses=None if self._advertise else [],
                discover_clients=False,
            )
        except OSError:
            await server.close()
            logger.exception(
                "Sendspin service: cannot start Sendspin server on %s:%s",
                self._host,
                self._port,
            )
            raise
        self._server = server
        self._stream_lock = asyncio.Lock()
        logger.info(
            "Sendspin service: Sendspin server listening on %s:%s",
            self._host,
            self._port,
        )

    def _on_client_event(self, server: AioSendspinServer, event: SendspinEvent) -> None:
        if isinstance(event, ClientConnectedEvent):
            task = asyncio.create_task(self._admit_player(server, event.client_id))
            self._client_tasks.add(task)
            task.add_done_callback(self._client_tasks.discard)

    @staticmethod
    async def _admit_player(server: AioSendspinServer, client_id: str) -> None:
        # Preserve Race Voice's open playback endpoint. SDK 5 connects using
        # encrypted unpaired access, which 9.x requires the server to approve.
        # The task runs after the synchronous client-attachment event returns.
        client = server.get_client(client_id)
        security = client.connection_security if client is not None else None
        if security is not None and security.psk_category is PskCategory.SENTINEL:
            try:
                await server.trust_unpaired(client_id)
            except Exception:
                logger.exception(
                    "Sendspin service: could not admit player %s", client_id
                )

    async def _close_server(self) -> None:
        tasks = list(self._client_tasks)
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        self._client_tasks.clear()
        if self._server is not None:
            await self._stop_stream()
            await self._server.close()
            self._server = None
        self._stream_lock = None
        self._stream_group = None
        self._stream = None
        self._next_play_start_us = None

    async def _stop_stream(self) -> None:
        await self._interrupt_stream(clear_client_audio=True)

    async def _stop_stream_locked(self, *, stop_all_client_groups: bool) -> None:
        self._cancel_idle_stop()

        server = self._server
        group = self._stream_group
        self._stream = None
        self._stream_group = None
        self._next_play_start_us = None
        if server is None:
            return
        if stop_all_client_groups:
            stop_tasks = [client.group.stop() for client in server.connected_clients]
            if stop_tasks:
                await asyncio.gather(*stop_tasks, return_exceptions=True)
        elif group is not None:
            await group.stop()

    async def _interrupt_stream(self, *, clear_client_audio: bool) -> None:
        """Immediately interrupt active playback, even while audio is being queued."""
        self._cancel_idle_stop()

        server = self._server
        stream = self._stream
        group = self._stream_group
        self._stream = None
        self._stream_group = None
        self._next_play_start_us = None

        if clear_client_audio and stream is not None and not stream.is_stopped:
            stream.clear()
        if stream is not None and not stream.is_stopped:
            stream.stop()

        if server is None:
            return

        groups = {client.group for client in server.connected_clients}
        if group is not None:
            groups.add(group)
        if groups:
            await asyncio.gather(
                *(group.stop() for group in groups),
                return_exceptions=True,
            )

    async def _append_to_stream(
        self,
        clips: list[_WavClip],
        expires_at: float | None,
        play_at: float | None,
        duration_s: float,
        volume: float,
    ) -> None:
        lock = self._stream_lock
        if lock is None:
            logger.warning("Sendspin service: Sendspin stream lock not ready")
            return
        async with lock:
            self._cancel_idle_stop()
            await self._append_to_stream_locked(
                clips, expires_at, play_at, duration_s, volume
            )

    async def _append_to_stream_locked(
        self,
        clips: list[_WavClip],
        expires_at: float | None,
        play_at: float | None,
        duration_s: float,
        volume: float,
    ) -> None:
        server = self._server
        if server is None:
            return
        clients = server.connected_clients
        if not clients:
            logger.info(
                "Sendspin service: no Sendspin clients connected - audio dropped"
            )
            return

        group, stream = await self._ensure_stream()
        if group is None or stream is None:
            return

        play_start_us = self._next_play_start_us
        now_us = server.clock.now_us()
        stream_options = _StreamOptions(max_buffer_us=_BUFFER_LIMIT_US, volume=volume)
        if play_at is not None:
            play_start_us = _scheduled_play_start_us(play_at, now_us)
            stream_options = _StreamOptions(
                max_buffer_us=_scheduled_buffer_limit_us(
                    play_start_us, now_us, duration_s
                ),
                full_clip=True,
                volume=volume,
            )
        elif play_start_us is None or play_start_us <= now_us:
            play_start_us = now_us + _group_lead_time_us(group)
        if self._would_start_after_expiry(play_start_us, now_us, expires_at):
            logger.info(
                "Sendspin service dropped stale audio before Sendspin scheduling"
            )
            return

        async def sync_clients() -> int:
            return await self._sync_connected_clients(group)

        try:
            play_end_us, streamed_count, client_count = await self._queue_wav_paths(
                stream,
                clips,
                play_start_us=play_start_us,
                sync_clients=sync_clients,
                options=stream_options,
            )
            client_count = max(client_count, await sync_clients())
            if streamed_count:
                logger.info(
                    "Sendspin service: queued %d WAV(s) to %d client(s)",
                    streamed_count,
                    client_count,
                )
                self._schedule_idle_stop(server.clock, group, play_end_us)
        except StreamStoppedError:
            logger.info("Sendspin service: Sendspin stream stopped")
            self._stream = None
            self._stream_group = None
            self._next_play_start_us = None

    async def _queue_wav_paths(
        self,
        stream: _PushStream,
        clips: list[_WavClip],
        *,
        play_start_us: int,
        sync_clients: Callable[[], Awaitable[int]],
        options: _StreamOptions,
    ) -> tuple[int, int, int]:
        play_end_us = play_start_us
        streamed_count = 0
        client_count = 0
        next_play_start_us: int | None = play_start_us
        for clip in clips:
            next_play_start_us, clip_end_us, client_count = await _stream_wav(
                stream,
                clip,
                play_start_us=next_play_start_us,
                sync_clients=sync_clients,
                options=options,
            )
            if clip_end_us is not None:
                play_end_us = clip_end_us
                self._next_play_start_us = clip_end_us
                streamed_count += 1
        return play_end_us, streamed_count, client_count

    async def _ensure_stream(self) -> tuple[SendspinGroup | None, _PushStream | None]:
        server = self._server
        if server is None or not server.connected_clients:
            return None, None
        if (
            self._stream_group is not None
            and self._stream is not None
            and not self._stream.is_stopped
        ):
            return self._stream_group, self._stream

        group = server.connected_clients[0].group
        await self._sync_connected_clients(group)
        stream = group.start_stream()
        self._stream_group = group
        self._stream = stream
        self._next_play_start_us = None
        return group, stream

    def _cancel_idle_stop(self) -> None:
        task = self._idle_stop_task
        if task is not None and not task.done() and task is not asyncio.current_task():
            task.cancel()
        if self._idle_stop_task is task:
            self._idle_stop_task = None

    def _schedule_idle_stop(
        self, clock: _SendspinClock, group: SendspinGroup, play_end_us: int
    ) -> None:
        self._cancel_idle_stop()
        self._idle_stop_task = asyncio.create_task(
            self._stop_stream_when_idle(clock, group, play_end_us)
        )

    @staticmethod
    def _would_start_after_expiry(
        play_start_us: int, now_us: int, expires_at: float | None
    ) -> bool:
        if expires_at is None:
            return False
        scheduled_delay_s = max(0.0, (play_start_us - now_us) / 1_000_000)
        return time.monotonic() + scheduled_delay_s > expires_at

    async def _stop_stream_when_idle(
        self, clock: _SendspinClock, group: SendspinGroup, play_end_us: int
    ) -> None:
        try:
            while True:
                if self._stream_group is not group:
                    return
                await self._sync_connected_clients(group)
                delay_s = (play_end_us - clock.now_us()) / 1_000_000
                if delay_s <= 0:
                    break
                await asyncio.sleep(min(delay_s, _LATE_JOIN_SYNC_INTERVAL_S))
            lock = self._stream_lock
            if lock is None:
                return
            async with lock:
                if (
                    self._stream_group is group
                    and self._next_play_start_us == play_end_us
                ):
                    await self._stop_stream_locked(stop_all_client_groups=False)
        except asyncio.CancelledError:
            pass

    async def _sync_connected_clients(self, group: SendspinGroup) -> int:
        server = self._server
        if server is None:
            return 0
        for client in server.connected_clients:
            if client.group is not group:
                await group.add_client(client)
                logger.info(
                    "Sendspin service: added late Sendspin client to active stream: %s",
                    client.client_id,
                )
        return sum(1 for client in group.clients if client.is_connected)


def _load_identity(state_dir: Path) -> Identity:
    """Persist the server identity across service restarts and package updates."""
    state_dir.mkdir(parents=True, exist_ok=True)
    path = state_dir / "identity.key"
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        return Identity.from_private_bytes(path.read_bytes())
    identity = Identity.generate()
    with os.fdopen(fd, "wb") as file:
        file.write(identity.private_bytes)
    return identity


def _handle_loop_exception(
    loop: asyncio.AbstractEventLoop, context: dict[str, object]
) -> None:
    """Ignore expected connection errors and delegate other loop exceptions."""
    exception = context.get("exception")
    if isinstance(exception, ConnectionError):
        logger.debug(
            "Sendspin service: suppressed loop connection error", exc_info=exception
        )
        return
    loop.default_exception_handler(context)


def _group_lead_time_us(group: SendspinGroup) -> int:
    """Return the max required lead time across all players in the group.

    Falls back to _INITIAL_PLAYBACK_DELAY_S when the player role or timing
    data is unavailable (e.g. old clients or before the role is negotiated).
    Result is in microseconds.
    """
    floor = int(_INITIAL_PLAYBACK_DELAY_S * 1_000_000)
    try:
        role = group.group_role("player")
        if role is None:
            return floor
        lead_times = [
            active_role.get_required_lead_time_us()
            for client in role.get_player_clients()
            for active_role in client.active_roles
            if hasattr(active_role, "get_required_lead_time_us")
        ]
        return max(floor, *lead_times) if lead_times else floor
    except Exception:
        return floor


def _scheduled_play_start_us(play_at: float, now_us: int) -> int:
    """Map a process-local monotonic target time to the Sendspin clock."""
    clock_offset_us = now_us - int(time.monotonic() * 1_000_000)
    requested_start_us = int(play_at * 1_000_000) + clock_offset_us
    minimum_start_us = now_us + int(_MIN_SCHEDULE_DELAY_S * 1_000_000)
    return max(requested_start_us, minimum_start_us)


def _scheduled_buffer_limit_us(
    play_start_us: int, now_us: int, duration_s: float
) -> int:
    """Allow future scheduled clips to be fully queued before playback starts."""
    scheduled_delay_us = max(0, play_start_us - now_us)
    duration_us = int(duration_s * 1_000_000)
    return max(
        _BUFFER_LIMIT_US,
        scheduled_delay_us + duration_us + _SCHEDULED_BUFFER_MARGIN_US,
    )


async def _stream_wav(
    stream: _PushStream,
    clip: _WavClip,
    *,
    play_start_us: int | None,
    sync_clients: Callable[[], Awaitable[int]],
    options: _StreamOptions,
) -> tuple[int | None, int | None, int]:
    client_count = await sync_clients()
    audio_format = clip.audio_format
    pcm_data = clip.pcm_data
    if not pcm_data:
        logger.warning("Sendspin service: empty WAV skipped: %s", clip.name)
        return play_start_us, None, client_count
    bytes_per_frame = audio_format.channels * (audio_format.bit_depth // 8)
    if len(pcm_data) % bytes_per_frame:
        logger.warning("Sendspin service: misaligned WAV skipped: %s", clip.name)
        return play_start_us, None, client_count
    pcm_data = _scale_pcm(pcm_data, audio_format.bit_depth // 8, options.volume)
    if options.full_clip:
        chunk_bytes = len(pcm_data)
    else:
        chunk_frames = max(1, int(audio_format.sample_rate * _CHUNK_DURATION_S))
        chunk_bytes = chunk_frames * bytes_per_frame
    play_end_us: int | None = None

    for offset in range(0, len(pcm_data), chunk_bytes):
        if stream.is_stopped:
            return play_start_us, play_end_us, client_count
        client_count = await sync_clients()
        await stream.sleep_to_limit_buffer(max_buffer_us=options.max_buffer_us)
        chunk = pcm_data[offset : offset + chunk_bytes]
        stream.prepare_audio(chunk, audio_format, channel_id=MAIN_CHANNEL)
        chunk_start_us = await stream.commit_audio(play_start_us=play_start_us)
        frames_in_chunk = len(chunk) // bytes_per_frame
        chunk_duration_us = int(frames_in_chunk * 1_000_000 / audio_format.sample_rate)
        play_end_us = chunk_start_us + chunk_duration_us
        play_start_us = None
    return play_start_us, play_end_us, client_count


def _scale_pcm(pcm_data: bytes, sample_width: int, volume: float) -> bytes:
    """Apply linear gain to PCM bytes without changing cached WAV files."""
    volume = max(0.0, min(1.0, volume))
    if volume == 1.0:
        return pcm_data
    if volume <= 0.0:
        return bytes(len(pcm_data))
    if sample_width == 2:
        return _scale_pcm_int(pcm_data, "<i2", "<f4", volume)
    if sample_width == 3:
        return _scale_pcm_int24(pcm_data, volume)
    if sample_width == 4:
        return _scale_pcm_int(pcm_data, "<i4", "<f8", volume)
    logger.warning("Sendspin service: cannot apply volume to %s-byte PCM", sample_width)
    return pcm_data


def _scale_pcm_int(
    pcm_data: bytes,
    sample_dtype_name: str,
    calc_dtype_name: str,
    volume: float,
) -> bytes:
    sample_dtype = np.dtype(sample_dtype_name)
    calc_dtype = np.dtype(calc_dtype_name)
    sample_range = np.iinfo(sample_dtype)
    samples = np.frombuffer(pcm_data, dtype=sample_dtype)
    scaled = np.clip(
        samples.astype(calc_dtype) * volume,
        sample_range.min,
        sample_range.max,
    ).astype(sample_dtype)
    return scaled.tobytes()


def _scale_pcm_int24(pcm_data: bytes, volume: float) -> bytes:
    raw = np.frombuffer(pcm_data, dtype=np.uint8).reshape(-1, 3)
    samples = (
        raw[:, 0].astype(np.int32)
        | (raw[:, 1].astype(np.int32) << 8)
        | (raw[:, 2].astype(np.int32) << 16)
    )
    samples = np.where(samples & 0x800000, samples - 0x1000000, samples)
    scaled = np.clip(
        (samples.astype(np.float32) * volume).astype(np.int32),
        -0x800000,
        0x7FFFFF,
    )
    packed = np.where(scaled < 0, scaled + 0x1000000, scaled).astype(np.uint32)
    output = np.empty((len(packed), 3), dtype=np.uint8)
    output[:, 0] = packed & 0xFF
    output[:, 1] = (packed >> 8) & 0xFF
    output[:, 2] = (packed >> 16) & 0xFF
    return output.tobytes()


def _read_wav_clips(wav_items: list[WavItem]) -> list[_WavClip]:
    """Read WAV files once and return clips ready for scheduling and streaming."""
    return [clip for item in wav_items if (clip := _read_wav(item))]


def _read_wav(item: WavItem) -> _WavClip | None:
    """Read a WAV item as PCM bytes plus metadata for Sendspin playback."""
    try:
        wav_source = io.BytesIO(item.data) if item.data is not None else item.path
        if wav_source is None:
            logger.warning(
                "Sendspin service: WAV item has no data or path: %s",
                item.name,
            )
            return None
        with wave.open(wav_source, "rb") as wav_file:
            sample_width = wav_file.getsampwidth()
            if sample_width not in {2, 3, 4}:
                logger.warning(
                    "Sendspin service: unsupported WAV sample width %s: %s",
                    sample_width,
                    item.name,
                )
                return None
            frame_count = wav_file.getnframes()
            sample_rate = wav_file.getframerate()
            audio_format = AudioFormat(
                sample_rate=sample_rate,
                bit_depth=sample_width * 8,
                channels=wav_file.getnchannels(),
            )
            return _WavClip(
                name=item.name,
                audio_format=audio_format,
                pcm_data=wav_file.readframes(frame_count),
                duration_s=frame_count / sample_rate,
            )
    except Exception:
        logger.exception("Sendspin service: cannot read WAV: %s", item.name)
        return None
