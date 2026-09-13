"""Exercise plugin uploads against the real HTTP service without audio hardware."""

# ruff: noqa: PT009, SLF001

from __future__ import annotations

import asyncio
import contextlib
import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import Mock, patch

from aiohttp import FormData
from aiohttp.test_utils import TestClient, TestServer

from sendspin_service.audio_queue import Priority
from sendspin_service.server import SendspinService, ServiceConfig, _create_app
from tests.test_sendspin_health import output

_DEMO_WAV = (
    Path(__file__).resolve().parents[1]
    / "custom_plugins/race_voice/assets/moavii-foreign.wav"
)


class SendspinUploadTests(unittest.IsolatedAsyncioTestCase):
    """Cover streaming, old-service compatibility and upload validation."""

    async def asyncSetUp(self) -> None:
        """Run the HTTP application with a recording queue and no player sockets."""
        self.queue = Mock()
        with (
            patch("sendspin_service.server.AudioQueue", return_value=self.queue),
            patch("sendspin_service.server.SendSpinServer") as backend,
        ):
            backend.return_value.connected_client_count.return_value = 0
            self.service = SendspinService(ServiceConfig())
        self.http = await self.enterAsyncContext(
            TestClient(TestServer(_create_app(self.service)))
        )
        self.adapter = output.SendspinServiceClient(
            service_url=lambda: str(self.http.make_url("/")), timeout_s=lambda: 5.0
        )
        self.directory = Path(self.enterContext(TemporaryDirectory()))

    async def test_complete_demo_streams_without_base64(self) -> None:
        """Preserve every byte of the full 132-second track using a raw upload."""
        path = _DEMO_WAV
        await asyncio.to_thread(self.adapter.health)
        with (
            patch.object(
                self.adapter,
                "health",
                side_effect=AssertionError("repeated health check"),
            ),
            patch.object(
                self.adapter,
                "_wav_files",
                side_effect=AssertionError("base64 path used"),
            ),
        ):
            await asyncio.to_thread(
                self.adapter.play, "audio check", [path], Priority.HIGH
            )
        self.queue.enqueue.assert_called_once()
        job = self.queue.enqueue.call_args.kwargs
        self.assertEqual(job["wav_items"][0].data, path.read_bytes())
        self.assertEqual(job["wav_items"][0].name, path.name)
        self.assertEqual(job["priority"], Priority.HIGH)
        self.assertEqual(job["text"], "audio check")

    async def test_old_service_uses_json(self) -> None:
        """Servers without the capability continue receiving the original API."""
        self.service.health = Mock(return_value={"ok": True})
        path = self.directory / "older-service.wav"
        data = b"a" * (1024 * 1024)
        path.write_bytes(data)
        with patch.object(
            output, "_multipart_body", side_effect=AssertionError("raw upload used")
        ):
            await asyncio.to_thread(
                self.adapter.play, "legacy", [path], Priority.NORMAL
            )
        self.assertEqual(self.queue.enqueue.call_args.kwargs["wav_items"][0].data, data)

    async def test_multiple_files_and_metadata_survive_raw_upload(self) -> None:
        """Preserve clip order, Unicode filenames, volume and scheduling metadata."""
        paths = [self.directory / 'piloot "één".wav', self.directory / "lap.wav"]
        for path, data in zip(paths, (b"first", b"second"), strict=True):
            path.write_bytes(data)
        await asyncio.to_thread(
            self.adapter._post_json,
            "/v1/play",
            {"text": "pilot", "priority": "high", "volume": 0.4, "expiry_sec": 3},
            wav_paths=paths,
        )
        job = self.queue.enqueue.call_args.kwargs
        self.assertEqual(
            [item.name for item in job["wav_items"]], [p.name for p in paths]
        )
        self.assertEqual(
            [item.data for item in job["wav_items"]], [b"first", b"second"]
        )
        self.assertEqual(job["volume"], 0.4)
        self.assertEqual(job["expiry_sec"], 3)

    async def test_rejects_oversized_raw_upload(self) -> None:
        """Enforce the body limit without enqueuing partial audio."""
        self.service._config = ServiceConfig(max_body_bytes=1024)
        form = FormData()
        form.add_field("metadata", "{}")
        form.add_field("wav_files", b"a" * 2048, filename="too-large.wav")
        response = await self.http.post("/v1/play", data=form)
        self.assertEqual(response.status, 413)
        self.queue.enqueue.assert_not_called()

    async def test_invalid_metadata_does_not_queue_audio(self) -> None:
        """Reject malformed metadata before accepting any clips."""
        for metadata in ("not json", json.dumps([])):
            with self.subTest(metadata=metadata):
                form = FormData()
                form.add_field("metadata", metadata)
                form.add_field("wav_files", b"audio", filename="test.wav")
                response = await self.http.post("/v1/play", data=form)
                self.assertEqual(response.status, 400)
        self.queue.enqueue.assert_not_called()

    async def test_chunked_upload_still_enforces_body_limit(self) -> None:
        """Reject oversized uploads even when Content-Length is absent."""
        self.service._config = ServiceConfig(max_body_bytes=1024)
        path = self.directory / "too-large.wav"
        path.write_bytes(b"a" * 2048)
        with contextlib.ExitStack() as stack:
            chunks, headers = output._multipart_body(stack, b"{}", [path])
            del headers["Content-Length"]

            async def body():  # noqa: ANN202
                for chunk in chunks:
                    yield chunk

            response = await self.http.post("/v1/play", data=body(), headers=headers)
        self.assertEqual(response.status, 413)
        self.queue.enqueue.assert_not_called()

    async def test_timed_out_upload_is_not_retried(self) -> None:
        """An ambiguous timeout must not send a potentially queued clip twice."""
        path = self.directory / "timeout.wav"
        path.write_bytes(b"a" * (1024 * 1024))
        with (
            patch.object(
                self.adapter, "health", return_value={"supports_multipart_play": True}
            ),
            patch.object(
                output.urllib.request, "urlopen", side_effect=TimeoutError
            ) as request,
            self.assertLogs(output.logger, level="ERROR"),
        ):
            await asyncio.to_thread(self.adapter.play, "test", [path], Priority.NORMAL)
        request.assert_called_once()
        self.queue.enqueue.assert_not_called()
