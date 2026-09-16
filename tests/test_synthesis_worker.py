"""Exercise real child isolation, bounded work and supervisor recovery."""

# ruff: noqa: PT009, PT027

from __future__ import annotations

import asyncio
import os
import time
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from sendspin_service.synthesis.synthesis import (
    SynthesisBusyError,
    SynthesisWorker,
    WorkerUnavailableError,
)


class WorkerTests(unittest.IsolatedAsyncioTestCase):
    """Keep a live asyncio loop running while a separate Python process blocks."""

    async def asyncSetUp(self) -> None:
        """Create an isolated supervisor with a predictable child implementation."""
        root = Path(self.enterContext(TemporaryDirectory()))
        self.worker = SynthesisWorker(
            root,
            max_pending=2,
            operation_timeout=1,
            worker_module="tests.helpers.synthesis_worker_probe",
        )
        self.addAsyncCleanup(self.worker.close)

    def submit(
        self, payload: dict, *, ttl: float = 2, priority: int = 3
    ) -> asyncio.Task:
        """Use caller-owned deadlines and real supervisor scheduling."""
        return asyncio.create_task(
            self.worker.request(
                payload,
                deadline=time.monotonic() + ttl,
                priority=priority,
            )
        )

    async def test_worker_is_a_reused_process_and_loop_keeps_ticking(self) -> None:
        """Blocking inference does not stop the control/playback event loop."""
        first = await self.submit({})
        work = self.submit({"delay": 0.15})
        ticks = 0
        while not work.done():
            await asyncio.sleep(0.005)
            ticks += 1
        second = await work
        self.assertNotEqual(first["pid"], os.getpid())
        self.assertEqual(first["pid"], second["pid"])
        self.assertGreaterEqual(ticks, 10)
        self.assertEqual(second["count"], 2)

    async def test_identical_callers_share_one_operation(self) -> None:
        """Local/cloud/listener fan-out does not repeat the expensive inference."""
        first, second = await asyncio.gather(
            self.submit({"delay": 0.05}),
            self.submit({"delay": 0.05}),
        )
        self.assertEqual(first, second)
        next_result = await self.submit({})
        self.assertEqual(next_result["count"], 2)

    async def test_cancelling_one_subscriber_preserves_other_subscriber(self) -> None:
        """A personal stream leaving must not cancel another listener's speech."""
        first = self.submit({"delay": 0.1})
        second = self.submit({"delay": 0.1})
        await asyncio.sleep(0.02)
        first.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await first
        self.assertEqual((await second)["count"], 1)

    async def test_expired_pending_work_is_skipped(self) -> None:
        """No inference is spent on work after every interested caller expires."""
        active = self.submit({"delay": 0.15})
        await asyncio.sleep(0.03)
        expired = self.submit({"text": "old"}, ttl=0.02)
        with self.assertRaises(TimeoutError):
            await expired
        await active
        self.assertEqual((await self.submit({}))["count"], 2)

    async def test_overload_is_explicit_and_releases_capacity(self) -> None:
        """Bound pending unique work independently of the single running job."""
        active = self.submit({"delay": 0.15})
        await asyncio.sleep(0.03)
        pending = [self.submit({"text": str(n)}) for n in range(2)]
        await asyncio.sleep(0)
        with self.assertRaises(SynthesisBusyError):
            await self.submit({"text": "overflow"})
        await asyncio.gather(active, *pending)
        self.assertEqual((await self.submit({}))["count"], 4)

    async def test_crash_and_hang_allow_fresh_worker(self) -> None:
        """A broken pipe or operation timeout must not poison future requests."""
        first = await self.submit({})
        with self.assertRaises(WorkerUnavailableError):
            await self.submit({"crash": True})
        second = await self.submit({})
        self.assertNotEqual(first["pid"], second["pid"])
        with self.assertRaises(WorkerUnavailableError):
            await self.submit({"delay": 5})
        third = await self.submit({})
        self.assertNotEqual(second["pid"], third["pid"])

    async def test_close_settles_active_and_pending_callers(self) -> None:
        """Shutdown must reap the child and unblock every awaiting operation."""
        active = self.submit({"delay": 5}, ttl=10)
        await asyncio.sleep(0.05)
        pending = self.submit({"text": "pending"}, ttl=10)
        await asyncio.sleep(0)
        await self.worker.close()
        results = await asyncio.gather(active, pending, return_exceptions=True)
        self.assertTrue(all(isinstance(r, WorkerUnavailableError) for r in results))
        with self.assertRaises(WorkerUnavailableError):
            await self.submit({})

    async def test_signal_promotes_duplicate_waiting_background_work(self) -> None:
        """Background preparation must not trap a live countdown at low priority."""
        active = self.submit({"delay": 0.15})
        await asyncio.sleep(0.03)
        background = self.submit({"text": "countdown"}, priority=10)
        lap = self.submit({"text": "lap"}, priority=3)
        await asyncio.sleep(0)
        countdown = self.submit({"text": "countdown"}, priority=-1)
        await active
        self.assertEqual((await countdown)["count"], 2)
        self.assertEqual((await lap)["count"], 3)
        self.assertEqual((await background)["count"], 2)


if __name__ == "__main__":
    unittest.main()
