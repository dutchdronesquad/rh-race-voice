"""Check HTTP responsiveness while the playback backend is busy stopping."""

# Use the standard-library test runner.
# ruff: noqa: PT009

from __future__ import annotations

import asyncio
import json
import threading
import unittest
from unittest.mock import AsyncMock, Mock

from aiohttp.test_utils import make_mocked_request

from sendspin_service.server import _health, _play, _stop


class SendspinHttpTests(unittest.IsolatedAsyncioTestCase):
    """A slow stop must not prevent other HTTP requests from being handled."""

    async def test_play_and_health_respond_while_stop_is_pending(self) -> None:
        """Keep accepting audio and health checks until a slow stop completes."""
        loop = asyncio.get_running_loop()
        started = asyncio.Event()
        release = threading.Event()

        def slow_stop() -> dict[str, object]:
            loop.call_soon_threadsafe(started.set)
            if not release.wait(timeout=2):
                raise TimeoutError("Test did not release the stop operation")
            return {"stopped": True, "dropped": 3}

        service = Mock()
        service.stop.side_effect = slow_stop
        service.play.return_value = {"queued": True, "count": 1}
        service.health.return_value = {"ok": True}
        app = {"service": service}
        stop_task = asyncio.create_task(
            _stop(make_mocked_request("POST", "/v1/stop", app=app))
        )
        try:
            await asyncio.wait_for(started.wait(), timeout=1)
            self.assertFalse(stop_task.done())
            play_request = make_mocked_request("POST", "/v1/play", app=app)
            payload = {"wav_files": [{"name": "test.wav", "data": ""}]}
            play_request.read = AsyncMock(return_value=json.dumps(payload).encode())
            play_response = await asyncio.wait_for(_play(play_request), timeout=0.5)
            health_response = await asyncio.wait_for(
                _health(make_mocked_request("GET", "/health", app=app)),
                timeout=0.5,
            )
            self.assertEqual(play_response.status, 202)
            service.play.assert_called_once_with(payload)
            self.assertEqual(json.loads(health_response.text), {"ok": True})
            self.assertFalse(stop_task.done())
        finally:
            release.set()
            stop_response = await asyncio.wait_for(stop_task, timeout=1)
        self.assertEqual(stop_response.status, 200)
        self.assertEqual(
            json.loads(stop_response.text), {"stopped": True, "dropped": 3}
        )

    async def test_stop_error_returns_server_error(self) -> None:
        """Keep reporting backend failures as HTTP errors after offloading."""
        service = Mock()
        service.stop.side_effect = RuntimeError("backend unavailable")
        request = make_mocked_request("POST", "/v1/stop", app={"service": service})
        with self.assertLogs("sendspin_service.server", level="ERROR"):
            response = await _stop(request)
        self.assertEqual(response.status, 500)
        self.assertEqual(json.loads(response.text), {"error": "internal server error"})
