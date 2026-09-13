"""Check clear cancellation barriers and cleanup ordering on the real worker queue."""

# ruff: noqa: PT009, SLF001

from __future__ import annotations

import asyncio
import json
import time
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import AsyncMock

from sendspin_service.cache_commands import CacheCommands
from sendspin_service.speech import SpeechEngine
from sendspin_service.synthesis import SynthesisWorker
from tests.test_race_ingest import VOICE, until


class CacheCommandTests(unittest.IsolatedAsyncioTestCase):
    """Keep supervision and queueing real while controlling the child operation."""

    async def asyncSetUp(self) -> None:
        """Use a gated exchange without loading Piper or running filesystem deletion."""
        root = Path(self.enterContext(TemporaryDirectory()))
        self.worker = SynthesisWorker(root)
        self.addAsyncCleanup(self.worker.close)
        self.speech = SpeechEngine(self.worker)
        self.cache = CacheCommands(self.speech, AsyncMock())
        self.cache.reset("source")
        self.addAsyncCleanup(self.cache.close)
        self.snapshot = {"context": {"revision": 1}, "voice": VOICE, "pilots": []}
        self.command = {
            "version": "race-events/1",
            "session_id": "source",
            "context": {"revision": 1},
            "settings_revision": 1,
            "command_id": "source:command:1",
            "operation": "clear_cache",
        }

    async def test_cancel_before_start_releases_barrier_after_task_finishes(
        self,
    ) -> None:
        """A task cancelled before its coroutine starts cannot leave speech blocked."""
        self.worker._execute = AsyncMock()
        self.cache.submit(self.command, self.snapshot)
        self.cache.cancel()
        self.assertTrue(self.cache.clearing)
        await until(lambda: not self.cache.clearing)
        self.worker._execute.assert_not_called()
        self.assertEqual(
            self.cache.status(self.command["command_id"])["status"], "cancelled"
        )

    async def test_running_clear_retains_worker_subscriber_after_repeated_cancel(
        self,
    ) -> None:
        """State changes cannot release admission while isolated deletion continues."""
        release = asyncio.Event()

        async def execute(_request: bytes) -> dict:
            await release.wait()
            return {"cleared": 1}

        self.worker._execute = AsyncMock(side_effect=execute)
        self.cache.submit(self.command, self.snapshot)
        await until(lambda: self.worker._execute.called)
        self.cache.cancel()
        self.cache.reset("replacement")
        await asyncio.sleep(0)
        self.assertTrue(self.cache.clearing)
        self.assertEqual(self.worker._waiters, 1)
        release.set()
        await until(lambda: not self.cache.clearing)
        self.assertEqual(self.worker._waiters, 0)

    async def test_temporary_cleanup_yields_to_live_speech_and_preparation(
        self,
    ) -> None:
        """An earlier queued cleanup runs after both live and manual speech jobs."""
        release = asyncio.Event()
        executed = []

        async def execute(request: bytes) -> dict:
            payload = json.loads(request)
            executed.append(payload)
            if payload.get("text") == "active":
                await release.wait()
            return {"audio": b"wav", "cache_hit": True, "cleared": 1}

        self.worker._execute = execute
        active = asyncio.create_task(
            self.worker.request(
                {"text": "active"},
                deadline=time.monotonic() + 5,
            )
        )
        await until(lambda: bool(executed))
        self.cache.clear_temporary(VOICE)
        await until(lambda: self.worker._queue.qsize() == 1)
        prepared = self.speech.prepare(VOICE, [], is_current=lambda: True)
        preparation = asyncio.create_task(anext(prepared))
        live = asyncio.create_task(
            self.worker.request(
                {"text": "live"},
                deadline=time.monotonic() + 5,
                priority=0,
            )
        )
        await until(lambda: self.worker._queue.qsize() == 3)
        release.set()
        await asyncio.gather(active, preparation, live)
        await prepared.aclose()
        await until(self.cache._cleanup.done)
        self.assertEqual(executed[1]["text"], "live")
        self.assertEqual(executed[2]["operation"], "synthesize")
        self.assertEqual(executed[3]["operation"], "clear")
