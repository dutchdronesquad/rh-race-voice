"""Check bounded RAM reuse across worker traffic, model changes and cache clears."""

# ruff: noqa: PT009, SLF001

from __future__ import annotations

import asyncio
import json
import threading
import time
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from sendspin_service.synthesis.synthesis import SynthesisWorker, WorkerUnavailableError

REQUEST = {
    "operation": "synthesize",
    "model": "nl_NL-pim-medium",
    "speed": 1,
    "noise": 0.667,
    "noise_w": 0.8,
    "text": "Klaas,",
    "subdir": "precache/pilots",
    "mock_audio": True,
}


class ReusableAudioTests(unittest.IsolatedAsyncioTestCase):
    """Use a real worker pipe and fake model files without loading Piper."""

    async def asyncSetUp(self) -> None:
        """Keep model metadata and immutable WAV reads in the production path."""
        self.root = Path(self.enterContext(TemporaryDirectory()))
        models = self.root / "models"
        models.mkdir()
        self.model = models / f"{REQUEST['model']}.onnx"
        self.model.write_bytes(b"model one")
        self.config = self.model.with_suffix(".onnx.json")
        self.config.write_text("{}")
        self.worker = SynthesisWorker(
            self.root,
            max_cache_bytes=500,
            worker_module="tests.helpers.synthesis_worker_probe",
        )
        self.addAsyncCleanup(self.worker.close)

    async def request(self, **changes: object) -> dict:
        """Give each subscriber its own deadline and optional voice/text changes."""
        return await self.worker.request(
            {**REQUEST, **changes}, deadline=time.monotonic() + 5
        )

    async def test_reusable_audio_avoids_worker_and_preserves_immutable_bytes(
        self,
    ) -> None:
        """Only the initial request crosses the subprocess boundary."""
        first = await self.request()
        second = await self.request()
        self.assertTrue(second["memory_cache_hit"])
        self.assertIs(first["audio"], second["audio"])
        self.assertNotIn("model_signature", first)
        next_job = await self.worker.request({}, deadline=time.monotonic() + 5)
        self.assertEqual(next_job["count"], 2)

    async def test_cached_name_does_not_wait_for_busy_inference(self) -> None:
        """Playback preparation can fetch prepared names while Piper stays busy."""
        first = await self.request()
        entered = asyncio.Event()
        release = asyncio.Event()

        async def blocked(_encoded: bytes) -> dict:
            entered.set()
            await release.wait()
            return {}

        with patch.object(self.worker, "_execute", side_effect=blocked):
            active = asyncio.create_task(
                self.worker.request({}, deadline=time.monotonic() + 5)
            )
            await asyncio.wait_for(entered.wait(), 1)
            try:
                second = await self.request()
                self.assertIs(first["audio"], second["audio"])
                self.assertFalse(active.done())
            finally:
                release.set()
                await active

    async def test_model_or_configuration_changes_invalidate_memory(self) -> None:
        """Model fingerprints protect names even when a worker is already warm."""
        first = await self.request()
        self.model.write_bytes(b"replacement model")
        second = await self.request()
        self.config.write_text('{"revision": 2}')
        third = await self.request()
        self.assertEqual([first["count"], second["count"], third["count"]], [1, 2, 3])
        self.assertNotEqual(first["audio"], second["audio"])
        self.assertNotEqual(second["audio"], third["audio"])

    async def test_text_case_and_voice_tuning_remain_distinct(self) -> None:
        """RAM reuse must not hide pronunciation or voice-setting changes."""
        first = await self.request()
        upper = await self.request(text="KLAAS,")
        tuned = await self.request(speed=1.5)
        self.assertEqual([first["count"], upper["count"], tuned["count"]], [1, 2, 3])

    async def test_heat_cleanup_retains_names_but_full_clear_removes_them(self) -> None:
        """A heat change must not throw away reusable audio in RAM."""
        first = await self.request()
        await self.request(operation="clear", subdir="tmp")
        self.assertIs((await self.request())["audio"], first["audio"])
        await self.request(operation="clear", subdir="")
        self.assertEqual((await self.request())["count"], 4)

    async def test_byte_budget_evicts_least_recently_used_audio(self) -> None:
        """Two 204-byte clips fit; a third evicts the clip not recently used."""
        first = await self.request()
        await self.request(text="Bravo,")
        self.assertIs((await self.request())["audio"], first["audio"])
        await self.request(text="Charlie,")
        self.assertTrue((await self.request())["memory_cache_hit"])
        self.assertEqual((await self.request(text="Bravo,"))["count"], 4)
        self.assertLessEqual(self.worker._audio_cache_bytes, 500)

    async def test_active_heat_survives_other_reusable_audio_pressure(self) -> None:
        """Background names/countdowns cannot evict a resident active pilot."""
        self.worker.prioritize_audio([REQUEST])
        first = await self.request()
        for number in range(6):
            await self.request(text=f"Countdown {number}", subdir="precache/clock")
        self.assertIs((await self.request())["audio"], first["audio"])
        self.assertLessEqual(self.worker._audio_cache_bytes, 500)

    async def test_readiness_never_starts_worker_and_distinguishes_cold_cached_audio(
        self,
    ) -> None:
        """A cached WAV is not evidence that an inference session is warm."""
        status = await self.worker.audio_status(REQUEST["model"], [REQUEST])
        self.assertEqual(status, {"model_warm": False, "resident": [False]})
        self.assertIsNone(self.worker._process)
        await self.request()
        status = await self.worker.audio_status(REQUEST["model"], [REQUEST])
        self.assertEqual(status, {"model_warm": False, "resident": [True]})
        await self.request(operation="warmup")
        status = await self.worker.audio_status(REQUEST["model"], [REQUEST])
        self.assertEqual(status, {"model_warm": True, "resident": [True]})
        await self.request(operation="clear", subdir="")
        status = await self.worker.audio_status(REQUEST["model"], [REQUEST])
        self.assertEqual(status, {"model_warm": True, "resident": [False]})

    async def test_model_change_and_worker_failure_remove_ready_status(self) -> None:
        """Model replacement and failed workers cannot leave a ready badge."""
        await self.request(operation="warmup")
        await self.request()
        self.model.write_bytes(b"replaced model contents")
        status = await self.worker.audio_status(REQUEST["model"], [REQUEST])
        self.assertEqual(status, {"model_warm": False, "resident": [False]})
        with self.assertRaises(WorkerUnavailableError):  # noqa: PT027
            await self.worker.request({"crash": True}, deadline=time.monotonic() + 5)
        status = await self.worker.audio_status(REQUEST["model"], [REQUEST])
        self.assertEqual(status, {"model_warm": False, "resident": [False]})

    async def test_idle_worker_exit_cannot_leave_the_model_marked_warm(self) -> None:
        """Notice a dead idle subprocess even before another request discovers it."""
        await self.request(operation="warmup")
        self.worker._process.terminate()
        await self.worker._process.wait()
        status = await self.worker.audio_status(REQUEST["model"], [REQUEST])
        self.assertFalse(status["model_warm"])

    async def test_full_protected_cache_does_not_admit_background_entries(self) -> None:
        """Even a more recent non-heat entry loses to both protected entries."""
        self.worker.prioritize_audio([REQUEST, {**REQUEST, "text": "Bravo,"}])
        first = await self.request()
        second = await self.request(text="Bravo,")
        await self.request(text="Countdown", subdir="precache/clock")
        self.assertIs((await self.request())["audio"], first["audio"])
        self.assertIs((await self.request(text="Bravo,"))["audio"], second["audio"])
        self.assertEqual(len(self.worker._audio_cache), 2)

    async def test_heat_switch_releases_previous_names_without_clearing_them(
        self,
    ) -> None:
        """Preference changes do not synthesize or clear reusable disk/RAM audio."""
        self.worker.prioritize_audio([REQUEST])
        old = await self.request()
        new = await self.request(text="Bravo,")
        self.worker.prioritize_audio([{**REQUEST, "text": "Bravo,"}])
        self.assertIs((await self.request())["audio"], old["audio"])
        await self.request(text="Countdown", subdir="precache/clock")
        self.assertIs((await self.request(text="Bravo,"))["audio"], new["audio"])
        self.assertFalse((await self.request()).get("memory_cache_hit", False))

    async def test_large_heat_still_obeys_byte_and_entry_limits(self) -> None:
        """Protecting too many pilots cannot turn the cache into unbounded memory."""
        requests = [{**REQUEST, "text": f"Pilot {number},"} for number in range(8)]
        self.worker.prioritize_audio(requests)
        for request in requests:
            await self.request(text=request["text"])
        self.assertLessEqual(self.worker._audio_cache_bytes, 500)
        self.assertEqual(len(self.worker._audio_cache), 2)

    async def test_dynamic_lap_times_do_not_consume_reusable_audio_memory(self) -> None:
        """Unique lap times must not evict all pilot names during a race."""
        first = await self.request()
        for value in range(3):
            await self.request(text=f"23.{value}", subdir="tmp")
        self.assertIs((await self.request())["audio"], first["audio"])

    async def test_entry_limit_is_independent_of_audio_size(self) -> None:
        """Many tiny phrases must not grow the dictionary without a bound."""
        worker = SynthesisWorker(
            self.root,
            max_cache_entries=1,
            worker_module="tests.helpers.synthesis_worker_probe",
        )
        self.addAsyncCleanup(worker.close)
        await worker.request(REQUEST, deadline=time.monotonic() + 5)
        await worker.request(
            {**REQUEST, "text": "Bravo,"}, deadline=time.monotonic() + 5
        )
        fresh = await worker.request(REQUEST, deadline=time.monotonic() + 5)
        self.assertEqual(fresh["count"], 3)
        self.assertEqual(len(worker._audio_cache), 1)

    async def test_worker_failure_discards_resident_audio(self) -> None:
        """A replacement child must not inherit opaque results from a failed worker."""
        first = await self.request()
        with self.assertRaises(WorkerUnavailableError):  # noqa: PT027
            await self.worker.request({"crash": True}, deadline=time.monotonic() + 5)
        fresh = await self.request()
        self.assertEqual(fresh["count"], 1)
        self.assertNotEqual(first["pid"], fresh["pid"])

    async def test_clear_during_metadata_check_cannot_return_old_memory(self) -> None:
        """The off-loop file check must not let a concurrent clear resurrect audio."""
        await self.request()
        entered = threading.Event()
        release = threading.Event()
        self.addCleanup(release.set)
        signature = self.worker._model_signature

        def blocked(model: str) -> tuple | None:
            entered.set()
            if not release.wait(5):
                raise TimeoutError("Test did not release metadata check")
            return signature(model)

        with patch.object(self.worker, "_model_signature", side_effect=blocked):
            lookup = asyncio.create_task(self.request())
            async with asyncio.timeout(1):
                while not entered.is_set():  # noqa: ASYNC110
                    await asyncio.sleep(0.001)
            await self.request(operation="clear", subdir="")
            release.set()
            self.assertEqual((await lookup)["count"], 3)

    async def test_clear_prevents_an_old_shared_result_refilling_memory(self) -> None:
        """A subscriber joining an old job after clear inherits that job's epoch."""
        entered = asyncio.Event()
        release = asyncio.Event()
        exchange = self.worker._exchange

        async def blocked(process: asyncio.subprocess.Process, encoded: bytes) -> dict:
            if (
                json.loads(encoded).get("operation") == "synthesize"
                and not entered.is_set()
            ):
                entered.set()
                await release.wait()
            return await exchange(process, encoded)

        with patch.object(self.worker, "_exchange", side_effect=blocked):
            first = asyncio.create_task(self.request())
            await asyncio.wait_for(entered.wait(), 1)
            clear = asyncio.create_task(self.request(operation="clear", subdir=""))
            await asyncio.sleep(0)
            shared = asyncio.create_task(self.request())
            await asyncio.sleep(0)
            release.set()
            await asyncio.gather(first, clear, shared)
        self.assertEqual((await self.request())["count"], 3)
