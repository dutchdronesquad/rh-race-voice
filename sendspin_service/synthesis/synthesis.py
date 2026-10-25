"""Bounded asynchronous supervision of a persistent isolated synthesis worker."""

from __future__ import annotations

import asyncio
import contextlib
import json
import math
import sys
import time
from collections import OrderedDict
from dataclasses import dataclass, field
from pathlib import Path

from custom_plugins.race_voice.const import VOICE_MODELS

_REUSABLE_SUBDIRS = frozenset(
    {"precache/pilots", "precache/laps", "precache/clock", "precache/schedule"}
)


class WorkerUnavailableError(RuntimeError):
    """The current synthesis operation failed; future jobs can start a new worker."""


class SynthesisBusyError(RuntimeError):
    """Bounded worker admission is full; caller must apply its overload policy."""


@dataclass(order=True)
class _Job:
    priority: int
    order: int
    key: str = field(compare=False)
    request: bytes = field(compare=False)
    result: asyncio.Future = field(compare=False)
    waiters: int = field(default=0, compare=False)
    running: bool = field(default=False, compare=False)
    cache_epoch: int = field(default=0, compare=False)


@dataclass(frozen=True)
class _CachedAudio:
    model: str
    signature: tuple
    audio: bytes


class SynthesisWorker:
    """Share identical in-flight jobs and keep blocking work outside asyncio."""

    def __init__(  # noqa: PLR0913
        self,
        root: Path,
        *,
        max_pending: int = 8,
        max_waiters: int = 64,
        operation_timeout: float = 120,
        worker_module: str = "sendspin_service.synthesis.synthesis_worker",
        max_cache_bytes: int = 8 * 1024 * 1024,
        max_cache_entries: int = 128,
    ) -> None:
        """Configure a worker; construction does not load a model or spawn a child."""
        if max_pending < 1 or max_waiters < 1 or operation_timeout <= 0:
            raise ValueError("Worker limits must be positive")
        if max_cache_bytes < 0 or max_cache_entries < 1:
            raise ValueError("Audio cache limits must be nonnegative/positive")
        self._root = root.resolve()
        self._max_waiters = max_waiters
        self._timeout = operation_timeout
        self._module = worker_module
        self._queue: asyncio.PriorityQueue[_Job] = asyncio.PriorityQueue(max_pending)
        self._jobs: dict[str, _Job] = {}
        self._runner: asyncio.Task | None = None
        self._process: asyncio.subprocess.Process | None = None
        self._order = 0
        self._waiters = 0
        self._closed = False
        self._max_cache_bytes = max_cache_bytes
        self._max_cache_entries = max_cache_entries
        self._audio_cache: OrderedDict[str, _CachedAudio] = OrderedDict()
        self._audio_cache_bytes = 0
        self._cache_epoch = 0
        self._preferred_audio: set[str] = set()
        self._warm_model: tuple[str, tuple] | None = None

    def prioritize_audio(self, requests: list[dict]) -> None:
        """Prefer the active heat's reusable segments without generating any audio."""
        self._preferred_audio = {
            json.dumps(request, sort_keys=True, allow_nan=False, ensure_ascii=False)
            for request in requests
        }

    async def audio_status(self, model: str, requests: list[dict]) -> dict:
        """Inspect actual residency and model warmth without starting the worker."""
        epoch = self._cache_epoch
        signature = await asyncio.to_thread(self._model_signature, model)
        resident = []
        for request in requests:
            key = json.dumps(
                request, sort_keys=True, allow_nan=False, ensure_ascii=False
            )
            cached = self._audio_cache.get(key)
            resident.append(
                not self._closed
                and epoch == self._cache_epoch
                and signature is not None
                and cached is not None
                and cached.model == model
                and cached.signature == signature
            )
        return {
            "model_warm": self._model_is_warm(model, signature),
            "resident": resident,
        }

    def _model_is_warm(self, model: str, signature: tuple | None) -> bool:
        return (
            not self._closed
            and self._process is not None
            and self._process.returncode is None
            and signature is not None
            and self._warm_model == (model, signature)
        )

    async def request(
        self,
        payload: dict,
        *,
        deadline: float,
        priority: int = 3,
    ) -> dict:
        """Await a shared result only while this caller's own deadline remains valid."""
        if self._closed:
            raise WorkerUnavailableError("Synthesis worker is closed")
        if not math.isfinite(deadline) or deadline <= time.monotonic():
            raise TimeoutError("Synthesis request is already expired")
        if self._waiters >= self._max_waiters:
            raise SynthesisBusyError("Too many synthesis subscribers")
        key = json.dumps(payload, sort_keys=True, allow_nan=False, ensure_ascii=False)
        encoded = (key + "\n").encode()
        if len(encoded) > 32_768:
            raise ValueError("Synthesis request is too large")
        if payload.get("operation") == "clear" and not payload.get("subdir"):
            self._invalidate_audio(payload.get("model"))
        if key in self._audio_cache:
            cached = await self._cached_result(key, deadline)
            if cached is not None:
                return cached
        return await self._await_job(
            payload, key, encoded, deadline=deadline, priority=priority
        )

    async def _await_job(
        self,
        payload: dict,
        key: str,
        encoded: bytes,
        *,
        deadline: float,
        priority: int,
    ) -> dict:
        job = self._jobs.get(key)
        if job is None:
            if self._queue.full():
                raise SynthesisBusyError("Synthesis queue is full")
            self._order += 1
            future = asyncio.get_running_loop().create_future()
            # A cancelled/expired caller may leave no observer for a worker failure.
            future.add_done_callback(_observe_failure)
            job = _Job(
                priority,
                self._order,
                key,
                encoded,
                future,
                cache_epoch=self._cache_epoch,
            )
            self._jobs[key] = job
            self._queue.put_nowait(job)
        elif priority < job.priority:
            self._promote(job, priority)
        job.waiters += 1
        self._waiters += 1
        if self._runner is None:
            self._runner = asyncio.create_task(self._run(), name="race-voice-synthesis")
        try:
            async with asyncio.timeout(max(0, deadline - time.monotonic())):
                result = dict(await asyncio.shield(job.result))
            self._remember_result(key, payload, result, job.cache_epoch)
            return result
        finally:
            job.waiters -= 1
            self._waiters -= 1
            if not job.waiters and not job.running and not job.result.done():
                self._discard_pending(job)

    async def _cached_result(self, key: str, deadline: float) -> dict | None:
        cached = self._audio_cache[key]
        # Metadata checks stay off the loop, even for an in-memory hit. Keep
        # them under the same subscriber and deadline bounds as inference.
        self._waiters += 1
        try:
            async with asyncio.timeout(max(0, deadline - time.monotonic())):
                signature = await asyncio.to_thread(self._model_signature, cached.model)
        finally:
            self._waiters -= 1
        if self._closed:
            raise WorkerUnavailableError("Synthesis worker is closed")
        if self._audio_cache.get(key) is not cached:
            return None  # A clear/restart/eviction happened during the metadata check.
        if signature == cached.signature:
            self._audio_cache.move_to_end(key)
            return {
                "audio": cached.audio,
                "cache_hit": True,
                "memory_cache_hit": True,
                "model_warm": self._model_is_warm(cached.model, signature),
                "duration_ms": 0,
            }
        del self._audio_cache[key]
        self._audio_cache_bytes -= len(cached.audio)
        return None

    def _remember_result(
        self, key: str, payload: dict, result: dict, epoch: int
    ) -> None:
        signature = result.pop("model_signature", None)
        if (
            self._max_cache_bytes
            and signature is not None
            and epoch == self._cache_epoch
            and payload.get("operation") == "synthesize"
            and payload.get("subdir") in _REUSABLE_SUBDIRS
            and payload.get("model") in VOICE_MODELS
        ):
            self._remember_audio(
                key, payload["model"], tuple(tuple(s) for s in signature), result
            )

    def _model_signature(self, model: str) -> tuple | None:
        paths = (
            self._root / "models" / f"{model}{suffix}"
            for suffix in (".onnx", ".onnx.json")
        )
        try:
            return tuple(
                (stat.st_ino, stat.st_size, stat.st_mtime_ns)
                for path in paths
                for stat in (path.stat(),)
            )
        except OSError:
            return None

    def _remember_audio(
        self, key: str, model: str, signature: tuple, result: dict
    ) -> None:
        audio = result.get("audio")
        if not isinstance(audio, bytes) or len(audio) > self._max_cache_bytes:
            return
        previous = self._audio_cache.pop(key, None)
        if previous is not None:
            self._audio_cache_bytes -= len(previous.audio)
        while self._audio_cache and (
            len(self._audio_cache) >= self._max_cache_entries
            or self._audio_cache_bytes + len(audio) > self._max_cache_bytes
        ):
            victim = next(
                (key for key in self._audio_cache if key not in self._preferred_audio),
                None,
            )
            if victim is None:
                if key not in self._preferred_audio:
                    return
                # A heat larger than the budget still obeys the hard bounds.
                victim = next(iter(self._audio_cache))
            evicted = self._audio_cache.pop(victim)
            self._audio_cache_bytes -= len(evicted.audio)
        self._audio_cache[key] = _CachedAudio(model, signature, audio)
        self._audio_cache_bytes += len(audio)

    def _invalidate_audio(self, model: str | None = None) -> None:
        self._cache_epoch += 1
        for key, cached in list(self._audio_cache.items()):
            if model is None or cached.model == model:
                del self._audio_cache[key]
                self._audio_cache_bytes -= len(cached.audio)

    def _discard_pending(self, job: _Job) -> None:
        """Free admission immediately when a queued operation loses its last caller."""
        pending = []
        while not self._queue.empty():
            item = self._queue.get_nowait()
            self._queue.task_done()
            if item is not job:
                pending.append(item)
        for item in pending:
            self._queue.put_nowait(item)
        self._jobs.pop(job.key, None)
        job.result.cancel()

    def _promote(self, job: _Job, priority: int) -> None:
        job.priority = priority
        pending = []
        while not self._queue.empty():
            pending.append(self._queue.get_nowait())
            self._queue.task_done()
        for pending_job in pending:
            self._queue.put_nowait(pending_job)

    async def close(self) -> None:
        """Cancel pending work and reap the child; safe to call more than once."""
        self._closed = True
        self._invalidate_audio()
        if self._runner is not None:
            self._runner.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._runner
            self._runner = None
        for job in self._jobs.values():
            if not job.result.done():
                job.result.set_exception(
                    WorkerUnavailableError("Synthesis worker closed")
                )
        self._jobs.clear()
        while not self._queue.empty():
            self._queue.get_nowait()
            self._queue.task_done()
        await self._stop_process()

    async def _run(self) -> None:
        while True:
            job = await self._queue.get()
            job.running = True
            try:
                if not job.waiters:
                    job.result.cancel()
                    continue
                payload = json.loads(job.request)
                if payload.get("operation") in {"synthesize", "warmup"} and (
                    self._warm_model is not None
                    and self._warm_model[0] != payload.get("model")
                ):
                    self._warm_model = None
                result = await self._execute(job.request)
                if payload.get("operation") in {"synthesize", "warmup"}:
                    signature = result.get("model_signature")
                    self._warm_model = (
                        (payload["model"], tuple(tuple(s) for s in signature))
                        if result.get("model_ready") is True and signature is not None
                        else None
                    )
                job.result.set_result(result)
            except asyncio.CancelledError:
                job.result.set_exception(
                    WorkerUnavailableError("Synthesis worker closed")
                )
                raise
            except Exception as err:
                job.result.set_exception(err)
            finally:
                self._jobs.pop(job.key, None)
                self._queue.task_done()

    async def _execute(self, request: bytes) -> dict:
        try:
            async with asyncio.timeout(self._timeout):
                if self._process is None:
                    self._process = await asyncio.create_subprocess_exec(
                        sys.executable,
                        "-m",
                        self._module,
                        str(self._root),
                        stdin=asyncio.subprocess.PIPE,
                        stdout=asyncio.subprocess.PIPE,
                        # stderr is inherited, never an undrained pipe.
                        limit=65_536,
                    )
                return await self._exchange(self._process, request)
        except asyncio.CancelledError:
            await self._stop_process()
            raise
        except Exception as err:
            await self._stop_process()
            raise WorkerUnavailableError("Synthesis worker operation failed") from err

    async def _exchange(
        self, process: asyncio.subprocess.Process, request: bytes
    ) -> dict:
        if process.stdin is None or process.stdout is None:
            raise WorkerUnavailableError("Worker pipes unavailable")
        process.stdin.write(request)
        await process.stdin.drain()
        line = await process.stdout.readline()
        if not line:
            raise WorkerUnavailableError("Worker exited before replying")
        reply = json.loads(line)
        if not isinstance(reply, dict) or type(reply.get("ok")) is not bool:
            raise WorkerUnavailableError("Invalid worker reply")
        if not reply["ok"]:
            raise WorkerUnavailableError(str(reply.get("error", "Worker failed")))
        if not isinstance(reply.get("result"), dict):
            raise WorkerUnavailableError("Invalid worker result")
        result = reply["result"]
        if "path" in result:
            result["audio"] = await asyncio.to_thread(
                self._pin_audio, result.pop("path")
            )
        return result

    def _pin_audio(self, name: str) -> bytes:
        # This finishes before the next worker job can clear a cache directory.
        path = Path(name).resolve()
        if not path.is_relative_to(self._root):
            raise WorkerUnavailableError("Worker returned a path outside its cache")
        with path.open("rb") as handle:
            audio = handle.read(4 * 1024 * 1024 + 1)
        if len(audio) > 4 * 1024 * 1024:
            raise WorkerUnavailableError("Synthesized audio exceeds the per-clip limit")
        return audio

    async def _stop_process(self) -> None:
        self._warm_model = None
        self._invalidate_audio()
        process, self._process = self._process, None
        if process is None:
            return
        if process.returncode is None:
            with contextlib.suppress(ProcessLookupError):
                process.terminate()
        try:
            await asyncio.wait_for(process.wait(), timeout=2)
        except TimeoutError:
            with contextlib.suppress(ProcessLookupError):
                process.kill()
            await process.wait()


def _observe_failure(future: asyncio.Future) -> None:
    if not future.cancelled():
        future.exception()
