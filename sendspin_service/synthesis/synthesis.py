"""Bounded asynchronous supervision of a persistent isolated synthesis worker."""

from __future__ import annotations

import asyncio
import contextlib
import json
import math
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path


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


class SynthesisWorker:
    """Share identical in-flight jobs and keep blocking work outside asyncio."""

    def __init__(
        self,
        root: Path,
        *,
        max_pending: int = 8,
        max_waiters: int = 64,
        operation_timeout: float = 120,
        worker_module: str = "sendspin_service.synthesis.synthesis_worker",
    ) -> None:
        """Configure a worker; construction does not load a model or spawn a child."""
        if max_pending < 1 or max_waiters < 1 or operation_timeout <= 0:
            raise ValueError("Worker limits must be positive")
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
        job = self._jobs.get(key)
        if job is None:
            if self._queue.full():
                raise SynthesisBusyError("Synthesis queue is full")
            self._order += 1
            future = asyncio.get_running_loop().create_future()
            # A cancelled/expired caller may leave no observer for a worker failure.
            future.add_done_callback(_observe_failure)
            job = _Job(priority, self._order, key, encoded, future)
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
                return dict(await asyncio.shield(job.result))
        finally:
            job.waiters -= 1
            self._waiters -= 1

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
            try:
                if not job.waiters:
                    job.result.cancel()
                    continue
                result = await self._execute(job.request)
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
