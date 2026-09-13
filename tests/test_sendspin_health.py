"""Check diagnostics for independently deployed Sendspin services."""

# Use the standard-library test runner; no pytest dependency is needed.
# ruff: noqa: PT009, PT027

from __future__ import annotations

import importlib.util
import io
import json
import unittest
import urllib.error
from importlib.metadata import version
from pathlib import Path
from unittest.mock import Mock, patch

from sendspin_service.server import SendspinService, ServiceConfig

# Load the HTTP client without importing RotorHazard's plugin entry point.
_SPEC = importlib.util.spec_from_file_location(
    "race_voice_output",
    Path(__file__).resolve().parents[1] / "custom_plugins/race_voice/output.py",
)
assert _SPEC is not None  # noqa: S101
assert _SPEC.loader is not None  # noqa: S101
output = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(output)


class ServiceHealthTests(unittest.TestCase):
    """Cover backend requirements and legacy or unavailable health endpoints."""

    def test_release_numbers_can_differ(self) -> None:
        """Use backend metadata rather than requiring matching release versions."""
        for release in ("1.0.0", "99.0.0", "0.0.0+dev"):
            with self.subTest(release=release):
                self.assertIsNone(
                    output.service_version_warning(
                        {"version": release, "aiosendspin_version": "9.1.0"}
                    )
                )

    def test_incompatible_backend_has_actionable_warning(self) -> None:
        """Identify the actual dependency and the separate installation to update."""
        warning = output.service_version_warning({"aiosendspin_version": "6.0.5"})
        self.assertIn("6.0.5", warning)
        self.assertIn("9.0.0", warning)
        self.assertIn("separate sendspin-service", warning)
        self.assertIn("RotorHazard venv does not", warning)

    def test_missing_or_unrecognized_version_is_unknown(self) -> None:
        """Do not declare older or malformed service responses compatible."""
        for backend in (None, "", "unknown", "9.0.0rc1", 9, [9, 1, 0]):
            with self.subTest(backend=backend):
                self.assertIn(
                    "cannot verify",
                    output.service_version_warning({"aiosendspin_version": backend}),
                )

    def test_reads_current_endpoint_and_timeout(self) -> None:
        """Read current settings on each check and preserve legacy health data."""
        settings = {"url": "http://localhost:8766", "timeout": 1.0}
        client = output.SendspinServiceClient(
            service_url=lambda: settings["url"],
            timeout_s=lambda: settings["timeout"],
        )
        for port in (8766, 8767):
            settings.update(url=f"http://localhost:{port}/", timeout=2.0)
            with (
                self.subTest(port=port),
                patch.object(
                    output.urllib.request,
                    "urlopen",
                    return_value=io.BytesIO(json.dumps({"ok": True}).encode()),
                ) as request,
            ):
                self.assertEqual(client.health(), {"ok": True})
                self.assertEqual(
                    request.call_args.args[0].full_url,
                    f"http://localhost:{port}/health",
                )
                self.assertEqual(request.call_args.kwargs["timeout"], 2.0)

    def test_invalid_and_unreachable_responses_raise(self) -> None:
        """Failures must reach the UI instead of masquerading as healthy replies."""
        client = output.SendspinServiceClient(
            service_url=lambda: "http://localhost:8766", timeout_s=lambda: 1.0
        )
        for body, error in ((b"[]", TypeError), (b"not json", ValueError)):
            with (
                self.subTest(body=body),
                patch.object(
                    output.urllib.request, "urlopen", return_value=io.BytesIO(body)
                ),
                self.assertRaises(error),
            ):
                client.health()
        with (
            patch.object(
                output.urllib.request,
                "urlopen",
                side_effect=urllib.error.URLError("connection refused"),
            ),
            self.assertRaises(urllib.error.URLError),
        ):
            client.health()

    def test_service_reports_installed_dependency(self) -> None:
        """Report runtime metadata even when the service release is a dev build."""
        with (
            patch("sendspin_service.server.SendSpinServer") as backend,
            patch("sendspin_service.server.AudioQueue", return_value=Mock()),
        ):
            backend.return_value.connected_client_count.return_value = 0
            service = SendspinService(ServiceConfig())
            health = service.health()
        self.assertEqual(health["aiosendspin_version"], version("aiosendspin"))
        self.assertTrue(health["ok"])
        self.assertEqual(health["connected_clients"], 0)


if __name__ == "__main__":
    unittest.main()
