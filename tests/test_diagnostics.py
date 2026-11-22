"""Exercise capture storage, failure isolation and authenticated commands."""

# ruff: noqa: PT009, SLF001, S105, S106 -- test credentials

from __future__ import annotations

import asyncio
import io
import json
import os
import threading
import unittest
import zipfile
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import Mock, patch

from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from sendspin_service.__main__ import main
from sendspin_service.diagnostics import (
    BACKUP_COUNT,
    MAX_CAPTURE_CONTEXTS,
    MAX_QUEUE_RECORDS,
    DiagnosticCapture,
    _Writer,
    summarize,
)
from sendspin_service.diagnostics_routes import add_routes
from sendspin_service.race import telemetry
from sendspin_service.server import SendspinService, ServiceConfig, _create_app


def unpack(data: bytes) -> dict[str, bytes]:
    """Inspect actual exported ZIP entries rather than mocking archive creation."""
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        return {name: archive.read(name) for name in archive.namelist()}


class CaptureTests(unittest.TestCase):
    """Storage limits and broken disks must not block or break audio producers."""

    def setUp(self) -> None:
        """Use private temporary storage and preserve the process logger state."""
        self.root = Path(self.enterContext(TemporaryDirectory()))
        self.enterContext(patch.object(telemetry.logger, "propagate", new=False))
        self.capture = DiagnosticCapture(self.root / "diagnostics", {}, max_bytes=4096)
        self.addCleanup(self.capture.stop)

    def test_recording_resumes_without_erasing_previous_evidence(self) -> None:
        """A stopped service's last recording remains downloadable by its successor."""
        telemetry.record("before", "received")
        self.assertFalse(self.capture.directory.exists())
        started = self.capture.start()
        self.assertTrue(started["active"])
        self.assertEqual(started["capture_id"], self.capture.start()["capture_id"])
        telemetry.record("lap:1", "received", model="en_GB-alan-medium")
        telemetry.record(
            "lap:1", "output_played", destination="local", delivery="queued"
        )
        data = unpack(self.capture.export())
        self.assertTrue(json.loads(data["manifest.json"])["recording_active"])
        self.assertNotIn(b"before", data["events.jsonl"])
        self.assertIn(b'"clock_id"', data["events.jsonl"])
        self.capture.stop()
        successor = DiagnosticCapture(self.capture.directory, {})
        restored = unpack(successor.export())
        self.assertFalse(json.loads(restored["manifest.json"])["recording_active"])
        self.assertEqual(restored["events.jsonl"], data["events.jsonl"])
        with patch.object(telemetry, "CLOCK_ID", "next-process"):
            successor.start()
            self.addCleanup(successor.stop)
            telemetry.record("lap:2", "received")
            resumed = unpack(successor.export())
        self.assertIn(b"lap:1", resumed["events.jsonl"])
        self.assertIn(b"lap:2", resumed["events.jsonl"])
        manifest = json.loads(resumed["manifest.json"])
        self.assertEqual(manifest["clock_id"], "next-process")
        self.assertEqual(
            manifest["previous_captures"][0]["capture_id"], started["capture_id"]
        )
        self.assertIn(
            "queued, not audible", json.loads(data["summary.json"])["delivery_meaning"]
        )

    def test_rotation_and_privacy_bounds(self) -> None:
        """Keep only the retained window, excluding text, URLs and credentials."""
        self.capture.start()
        for index in range(300):
            telemetry.record(
                f"lap:{index}",
                "segment",
                request_ms=index,
                text="private pilot",
                token="secret-token",
                url="https://secret-token@host",
            )
        data = unpack(self.capture.export())
        files = {
            name: contents
            for name, contents in data.items()
            if name.startswith("events.")
        }
        self.assertLessEqual(len(files), BACKUP_COUNT + 1)
        self.assertLessEqual(sum(map(len, files.values())), 4096 * (BACKUP_COUNT + 1))
        raw = b"".join(data.values())
        self.assertNotIn(b"private pilot", raw)
        self.assertNotIn(b"secret-token", raw)
        self.assertNotIn(b'"event_id": "lap:0"', raw)
        self.assertIn(b'"event_id": "lap:299"', raw)

    def test_restart_history_stays_bounded_and_recovers_from_bad_metadata(self) -> None:
        """Repeated restarts and a damaged manifest must not erase race evidence."""
        self.capture.start()
        telemetry.record("first-race", "received")
        self.capture.stop()
        for _ in range(MAX_CAPTURE_CONTEXTS + 2):
            self.capture.start()
            self.capture.stop()
        data = unpack(self.capture.export())
        self.assertIn(b"first-race", data["events.jsonl"])
        self.assertEqual(
            len(json.loads(data["manifest.json"])["previous_captures"]),
            MAX_CAPTURE_CONTEXTS,
        )
        (self.capture.directory / "manifest.json").write_text("interrupted write")
        self.capture.start()
        telemetry.record("later-race", "received")
        data = unpack(self.capture.export())
        self.assertIn(b"first-race", data["events.jsonl"])
        self.assertIn(b"later-race", data["events.jsonl"])
        self.assertEqual(json.loads(data["manifest.json"])["previous_captures"], [])

    def test_disk_stall_only_drops_bounded_diagnostic_records(self) -> None:
        """The producer can finish while a writer is deliberately blocked on disk."""
        self.capture.start()

        def produce() -> None:
            for index in range(MAX_QUEUE_RECORDS + 20):
                telemetry.record(f"lap:{index}", "received")

        with self.capture._file_lock:
            producer = threading.Thread(target=produce)
            producer.start()
            producer.join(timeout=3)
            self.assertFalse(producer.is_alive(), "Logging waited for the disk writer")
            self.assertGreater(self.capture.status()["dropped_records"], 0)
            self.assertLessEqual(self.capture.records.qsize(), MAX_QUEUE_RECORDS)
        producer.join(timeout=3)
        self.capture.stop()

    def test_writer_failure_is_reported_and_recording_can_restart(self) -> None:
        """Disk errors remain diagnostics failures rather than playback exceptions."""
        self.capture.start()
        with patch.object(_Writer, "emit", side_effect=OSError("disk full")):
            telemetry.record("lap:1", "received")
            data = unpack(self.capture.export())
        self.assertEqual(self.capture.status()["storage_error"], "OSError")
        self.assertEqual(self.capture.status()["dropped_records"], 1)
        self.assertEqual(json.loads(data["summary.json"])["counts"], {})
        self.capture.stop()
        self.capture.start()
        telemetry.record("lap:2", "received")
        self.assertIn(b"lap:2", unpack(self.capture.export())["events.jsonl"])

    def test_summary_does_not_mix_host_clocks_or_invent_small_sample_tails(
        self,
    ) -> None:
        """Matching event IDs on two hosts do not imply matching monotonic clocks."""
        lines = [
            {"event_id": "lap:1", "stage": "received", "clock_id": "primary", "t": 100},
            {
                "event_id": "lap:1",
                "stage": "output_played",
                "clock_id": "cloud",
                "t": 5,
                "destination": "local",
            },
            {
                "event_id": "lap:1",
                "stage": "output_played",
                "clock_id": "primary",
                "t": 100.25,
                "destination": "local",
            },
        ]
        result = summarize(map(json.dumps, lines))
        metric = result["metrics"]["primary/local/output_played/receipt_to_queue_ms"]
        self.assertEqual(metric["p50_ms"], 250)
        self.assertIsNone(metric["p95_ms"])
        self.assertIsNone(metric["p99_ms"])
        self.assertFalse(any(name.startswith("cloud/") for name in result["metrics"]))


class DiagnosticApiTests(unittest.IsolatedAsyncioTestCase):
    """Use actual authenticated HTTP and the real management entry point."""

    async def asyncSetUp(self) -> None:
        """Keep audio and model loading inert while exercising administrative routes."""
        self.root = Path(self.enterContext(TemporaryDirectory()))
        self.token = "test-api-token"
        self.enterContext(patch.object(telemetry.logger, "propagate", new=False))
        with patch("sendspin_service.server.SendSpinServer", return_value=Mock()):
            self.service = SendspinService(
                ServiceConfig(
                    api_token=self.token,
                    race_cache_dir=self.root / "cache",
                    diagnostics_dir=self.root / "diagnostics",
                )
            )
        self.client = TestClient(TestServer(_create_app(self.service)))
        await self.client.start_server()
        self.addAsyncCleanup(self.client.close)
        self.headers = {"Authorization": f"Bearer {self.token}"}

    async def test_automatic_recording_requires_admin_token_to_export(self) -> None:
        """Recording starts with the service; listeners cannot read its private data."""
        for method, path in (
            ("get", "status"),
            ("get", "export"),
        ):
            response = await getattr(self.client, method)(f"/v2/diagnostics/{path}")
            self.assertEqual(response.status, 401)
        response = await self.client.get("/v2/diagnostics/status", headers=self.headers)
        self.assertTrue((await response.json())["active"])
        telemetry.record("lap:1", "received", api_token=self.token)
        response = await self.client.get("/v2/diagnostics/export", headers=self.headers)
        data = await response.read()
        self.assertEqual(response.status, 200)
        self.assertNotIn(self.token.encode(), b"".join(unpack(data).values()))
        self.assertTrue(self.service.diagnostics.status()["active"])
        await self.client.close()
        self.assertFalse(self.service.diagnostics.status()["active"])

    async def test_management_cli_round_trip_and_no_overwrite(self) -> None:
        """Operators can capture/export using local config without editing code."""
        env_file = self.root / "service.env"
        env_file.write_text(f'SENDSPIN_API_TOKEN="{self.token}"\n')
        common = ["--url", str(self.client.make_url("")), "--env-file", str(env_file)]
        with patch.dict(os.environ, {}, clear=True):
            telemetry.record("lap:1", "received")
            target = self.root / "capture.zip"
            command = ["diagnostics", "export", *common, "--output", str(target)]
            self.assertEqual(await asyncio.to_thread(main, command), 0)
            first = target.read_bytes()
            self.assertIn("manifest.json", unpack(first))
            self.assertEqual(await asyncio.to_thread(main, command), 1)
            self.assertEqual(target.read_bytes(), first)


class AutomaticStartupTests(unittest.IsolatedAsyncioTestCase):
    """Automatic diagnostics must never prevent the HTTP service starting."""

    async def test_unwritable_storage_keeps_service_available(self) -> None:
        """A broken diagnostic directory becomes status, not a service outage."""
        root = Path(self.enterContext(TemporaryDirectory()))
        blocked = root / "not-a-directory"
        blocked.write_text("existing file")
        capture = DiagnosticCapture(blocked, {})
        app = web.Application()
        add_routes(app, capture)
        async with TestClient(TestServer(app)) as client:
            response = await client.get("/v2/diagnostics/status")
            self.assertEqual(response.status, 200)
            status = await response.json()
            self.assertFalse(status["active"])
            self.assertEqual(status["storage_error"], "FileExistsError")
            self.assertEqual(blocked.read_text(), "existing file")
