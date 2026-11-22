"""Exercise CLI management without starting audio or changing an installation."""

# ruff: noqa: PT009

from __future__ import annotations

import io
import json
import os
import subprocess
import sys
import threading
import unittest
from contextlib import redirect_stdout
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from sendspin_service.__main__ import main


class ManagementTests(unittest.TestCase):
    """Run the actual entry point with unavailable audio dependencies."""

    def test_help_version_and_invalid_commands_never_import_runtime(self) -> None:
        """Bare invocation is safe and unknown commands fail before server import."""
        probe = """
import runpy, sys
for name in ('sendspin_service.server', 'aiosendspin', 'numpy', 'av', 'aiohttp'):
    sys.modules[name] = None
sys.argv = ['sendspin-service', *sys.argv[1:]]
runpy.run_module('sendspin_service', run_name='__main__')
"""
        for arguments, code, expected in (
            ([], 0, "Start the service in the foreground"),
            (["--help"], 0, "update"),
            (["--version"], 0, "service: 2.9.0"),
            (["version"], 0, "aiosendspin:"),
            (["status", "--help"], 0, "--url"),
            (["update", "--help"], 0, "--yes"),
            (["diagnostics", "--help"], 0, "export"),
            (["unknown"], 2, "invalid choice"),
            (["--port", "8766"], 2, "invalid choice"),
        ):
            with self.subTest(arguments=arguments):
                result = subprocess.run(  # noqa: S603
                    [sys.executable, "-c", probe, *arguments],
                    env={**os.environ, "SENDSPIN_SERVICE_VERSION": "2.9.0"},
                    capture_output=True,
                    text=True,
                    timeout=5,
                    check=False,
                )
                self.assertEqual(result.returncode, code, result.stderr)
                self.assertIn(expected, result.stdout + result.stderr)


class StatusTests(unittest.TestCase):
    """Query a loopback HTTP server with healthy, invalid and failed responses."""

    def setUp(self) -> None:
        """Serve health data from another thread, independent of the CLI."""
        self.content = json.dumps(
            {
                "ok": True,
                "version": "2.8.0",
                "aiosendspin_version": "10.2.2",
                "connected_players": 3,
            }
        ).encode()
        self.http_status = 200
        self.paths = []
        fixture = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self) -> None:
                fixture.paths.append(self.path)
                self.send_response(fixture.http_status)
                self.end_headers()
                self.wfile.write(fixture.content)

            def log_message(self, *_args: object) -> None:
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(thread.join, 3)
        self.addCleanup(self.server.shutdown)
        self.url = f"http://127.0.0.1:{self.server.server_port}"

    def run_status(self, *arguments: str) -> tuple[int, str]:
        """Capture command output without redirecting the HTTP server."""
        output = io.StringIO()
        with redirect_stdout(output):
            code = main(["status", *arguments])
        return code, output.getvalue()

    def test_running_and_installed_versions_are_distinct(self) -> None:
        """A process from before an update must report its actual running version."""
        with patch.dict(os.environ, {"SENDSPIN_SERVICE_VERSION": "2.9.0"}):
            code, output = self.run_status("--url", self.url, "--json")
        payload = json.loads(output)
        self.assertEqual(code, 0)
        self.assertEqual(payload["installed"]["service"], "2.9.0")
        self.assertEqual(payload["running"]["version"], "2.8.0")
        self.assertEqual(payload["running"]["connected_players"], 3)
        self.assertEqual(self.paths, ["/health"])

    def test_local_config_is_used_without_executing_shell(self) -> None:
        """Honor a custom API port and map a wildcard bind to loopback."""
        directory = Path(self.enterContext(TemporaryDirectory()))
        config = directory / "service.env"
        config.write_text(
            'SENDSPIN_INGEST_HOST="0.0.0.0"\n'
            f"SENDSPIN_INGEST_PORT={self.server.server_port}\n"
            "SENDSPIN_API_TOKEN=$(touch forbidden)\n"
        )
        with patch.dict(os.environ, {}, clear=True):
            code, output = self.run_status("--env-file", str(config))
        self.assertEqual(code, 0)
        self.assertIn("Running aiosendspin: 10.2.2", output)
        self.assertIn("Connected players: 3", output)
        self.assertNotIn("forbidden", output)

    def test_remote_proxy_prefix_is_preserved(self) -> None:
        """A remote service can live under a reverse-proxy path prefix."""
        code, _ = self.run_status("--url", self.url + "/audio/")
        self.assertEqual(code, 0)
        self.assertEqual(self.paths, ["/audio/health"])

    def test_invalid_or_unhealthy_http_responses_fail_cleanly(self) -> None:
        """Bad JSON, wrong services, oversized responses and HTTP failures return 1."""
        for status, content in (
            (200, b"not json"),
            (200, b"[]"),
            (200, b'{"ok": false}'),
            (200, b"x" * 65537),
            (503, b"unavailable"),
        ):
            with self.subTest(status=status, content=content[:20]):
                self.http_status, self.content = status, content
                code, output = self.run_status("--url", self.url, "--json")
                self.assertEqual(code, 1)
                self.assertFalse(json.loads(output)["reachable"])
                self.assertIn("error", json.loads(output))

    def test_network_failure_and_non_http_url_do_not_start_service(self) -> None:
        """Inspection errors produce a diagnostic and a nonzero exit code."""
        with patch(
            "sendspin_service.cli.urlopen", side_effect=TimeoutError("timed out")
        ):
            code, output = self.run_status("--url", self.url)
        self.assertEqual(code, 1)
        self.assertIn("unreachable", output)
        self.assertIn("systemctl status", output)
        code, output = self.run_status("--url", "file:///etc/passwd", "--json")
        self.assertEqual(code, 1)
        self.assertNotIn("root:", output)


class UpdateTests(unittest.TestCase):
    """Run an inert installer fixture; never invoke apt, sudo or systemctl."""

    def test_updater_preserves_confirmation_and_propagates_failure(self) -> None:
        """Default updates prompt; unattended and exact releases are explicit."""
        directory = Path(self.enterContext(TemporaryDirectory()))
        installer = directory / "installer.sh"
        record = directory / "arguments"
        installer.write_text(
            '#!/bin/bash\n[[ -z "${PYTHONHOME+x}${PYTHONPATH+x}" ]] || exit 9\n'
            'printf "%s\\n" "$@" > "$UPDATE_RECORD"\nexit 7\n'
        )
        with (
            patch("sendspin_service.cli.INSTALLER", installer),
            patch.dict(
                os.environ,
                {
                    "UPDATE_RECORD": str(record),
                    "PYTHONHOME": "/opt/sendspin-service/runtime",
                    "PYTHONPATH": "/opt/sendspin-service/app",
                },
            ),
        ):
            self.assertEqual(main(["update"]), 7)
            self.assertEqual(record.read_text().splitlines(), ["--latest"])
            self.assertEqual(main(["update", "v2.9.0", "--yes"]), 7)
            self.assertEqual(record.read_text().splitlines(), ["v2.9.0", "--yes"])

    def test_missing_installer_never_launches_a_process(self) -> None:
        """Docker and source installs receive guidance instead of running apt."""
        directory = Path(self.enterContext(TemporaryDirectory()))
        with (
            patch("sendspin_service.cli.INSTALLER", directory / "absent"),
            patch("sendspin_service.cli.subprocess.run") as run,
        ):
            self.assertEqual(main(["update"]), 1)
            run.assert_not_called()
