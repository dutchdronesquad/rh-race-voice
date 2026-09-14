"""Run gevent-isolated adapter regressions and the actual local HTTP pipeline."""

# ruff: noqa: PT009

from __future__ import annotations

import asyncio
import json
import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import Mock, patch

from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from sendspin_service.server import SendspinService, ServiceConfig, _create_app
from tests.test_race_ingest import Worker


class EventAdapterTests(unittest.IsolatedAsyncioTestCase):
    """Keep gevent's monkey patches out of the service and main test interpreter."""

    async def run_probe(self, *args: str) -> dict | None:
        """Reap the separate RH-style interpreter, including on timeout."""
        process = await asyncio.create_subprocess_exec(
            sys.executable,
            "-m",
            "tests.helpers.gevent_event_adapter_probe",
            *args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            async with asyncio.timeout(30):
                stdout, stderr = await process.communicate()
        finally:
            if process.returncode is None:
                process.kill()
                await process.wait()
        self.assertEqual(process.returncode, 0, stderr.decode())
        return json.loads(stdout) if stdout.strip() else None

    async def test_gevent_adapter_scenarios(self) -> None:
        """Check queue pressure, hub responsiveness, identity and reconnect recovery."""
        await self.run_probe()

    async def test_adapter_to_actual_service_and_playback(self) -> None:
        """Publish from patched RH into unpatched HTTP, speech and output planners."""
        root = Path(self.enterContext(TemporaryDirectory()))
        backend = Mock()
        backend.connected_client_count.return_value = 0
        worker = Worker()
        with (
            patch("sendspin_service.server.SendSpinServer", return_value=backend),
            patch(
                "sendspin_service.synthesis.synthesis.SynthesisWorker",
                return_value=worker,
            ),
        ):
            service = SendspinService(ServiceConfig(race_cache_dir=root))
            app = _create_app(service)

            async def playback(_request: web.Request) -> web.Response:
                return web.json_response({"count": backend.play.call_count})

            app.router.add_get("/test/playback", playback)
            async with TestClient(TestServer(app)) as client:
                result = await self.run_probe(str(client.make_url("")))
        self.assertEqual(result, {"connected": True, "tts_imported": False})
        self.assertGreaterEqual(backend.play.call_count, 2)
        clips = backend.play.call_args_list[0].args[0]
        self.assertEqual(len(clips), 3)
        self.assertEqual(worker.calls[0]["text"], "Alfa,")
        self.assertEqual(worker.calls[2]["text"], "twenty seconds")
        self.assertGreater(len(worker.calls), 3)
        self.assertEqual(worker.calls[-1]["operation"], "clear")
        self.assertTrue(
            any(call.get("subdir") == "precache/clock" for call in worker.calls)
        )
        self.assertTrue(worker.closed)
