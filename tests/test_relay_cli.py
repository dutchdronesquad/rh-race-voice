"""Verify relay_cli's env-file editing, token resolution, and dispatch."""

# ruff: noqa: PT009, PT027

from __future__ import annotations

import io
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from sendspin_service import relay_cli, server
from sendspin_service.relay_cli import _apply, _existing_value


class ApplyLinesTests(unittest.TestCase):
    """Exercise _apply/_existing_value as pure list-of-lines transforms."""

    def test_replaces_an_active_line(self) -> None:
        """An existing KEY=value line is replaced in place."""
        lines = ["SENDSPIN_RELAY_URL=old", "OTHER=kept"]
        result = _apply(lines, {"SENDSPIN_RELAY_URL": "new"})
        self.assertEqual(result, ["SENDSPIN_RELAY_URL=new", "OTHER=kept"])

    def test_replaces_a_commented_placeholder_line(self) -> None:
        """A shipped '# KEY=' placeholder is uncommented and filled in."""
        lines = ["# SENDSPIN_RELAY_URL=", "OTHER=kept"]
        result = _apply(lines, {"SENDSPIN_RELAY_URL": "new"})
        self.assertEqual(result, ["SENDSPIN_RELAY_URL=new", "OTHER=kept"])

    def test_appends_when_absent(self) -> None:
        """A key with no existing line, commented or not, is appended."""
        result = _apply(["OTHER=kept"], {"SENDSPIN_RELAY_URL": "new"})
        self.assertEqual(result, ["OTHER=kept", "SENDSPIN_RELAY_URL=new"])

    def test_existing_value_ignores_commented_lines(self) -> None:
        """A commented placeholder does not count as an existing value."""
        lines = ["# SENDSPIN_RELAY_URL=x"]
        self.assertIsNone(_existing_value(lines, "SENDSPIN_RELAY_URL"))

    def test_existing_value_reads_an_active_line(self) -> None:
        """An active line's value is returned verbatim."""
        value = _existing_value(["SENDSPIN_RELAY_URL=https://x"], "SENDSPIN_RELAY_URL")
        self.assertEqual(value, "https://x")


class RelayCommandTests(unittest.TestCase):
    """Exercise enable/disable/status end-to-end against a real temp file."""

    def setUp(self) -> None:
        """Point every command at a fresh temp env file."""
        directory = self.enterContext(TemporaryDirectory())
        self.env_file = Path(directory) / "sendspin-service"

    def _run(self, action: str, *args: str) -> str:
        buffer = io.StringIO()
        argv = [action, *args, "--env-file", str(self.env_file)]
        if action != "status":
            argv.append("--no-restart")
        with redirect_stdout(buffer):
            exit_code = relay_cli.main(argv)
        self.assertEqual(exit_code, 0)
        return buffer.getvalue()

    def test_enable_writes_url_and_a_generated_token(self) -> None:
        """Enable with no --token generates and persists one."""
        output = self._run("enable", "--url", "https://cloud.example.com")
        content = self.env_file.read_text()
        self.assertIn("SENDSPIN_RELAY_URL=https://cloud.example.com", content)
        self.assertIn("SENDSPIN_RELAY_TOKEN=", content)
        token = _existing_value(content.splitlines(), "SENDSPIN_RELAY_TOKEN")
        self.assertEqual(len(token), 64)
        self.assertIn(token, output)

    def test_enable_rejects_a_url_without_a_scheme(self) -> None:
        """A bare host:port is rejected instead of failing later at request time."""
        env_file = str(self.env_file)
        argv = ["enable", "--url", "cloud.example.com", "--env-file", env_file]
        with self.assertRaises(SystemExit) as raised:
            relay_cli.main(argv)
        self.assertEqual(raised.exception.code, 2)
        self.assertFalse(self.env_file.exists())

    def test_enable_reuses_an_existing_token(self) -> None:
        """A second enable call keeps the previously stored token."""
        self._run("enable", "--url", "https://a.example.com")
        lines = self.env_file.read_text().splitlines()
        first_token = _existing_value(lines, "SENDSPIN_RELAY_TOKEN")
        self._run("enable", "--url", "https://b.example.com")
        lines = self.env_file.read_text().splitlines()
        second_token = _existing_value(lines, "SENDSPIN_RELAY_TOKEN")
        self.assertEqual(first_token, second_token)

    def test_enable_rotate_token_replaces_an_existing_token(self) -> None:
        """--rotate-token forces a fresh token even when one is stored."""
        self._run("enable", "--url", "https://a.example.com")
        lines = self.env_file.read_text().splitlines()
        first_token = _existing_value(lines, "SENDSPIN_RELAY_TOKEN")
        self._run("enable", "--url", "https://a.example.com", "--rotate-token")
        lines = self.env_file.read_text().splitlines()
        second_token = _existing_value(lines, "SENDSPIN_RELAY_TOKEN")
        self.assertNotEqual(first_token, second_token)

    def test_enable_explicit_token_overrides_stored_value(self) -> None:
        """An explicit --token is used verbatim regardless of what's stored."""
        self._run("enable", "--url", "https://a.example.com", "--token", "stored")
        self._run("enable", "--url", "https://a.example.com", "--token", "explicit")
        lines = self.env_file.read_text().splitlines()
        token = _existing_value(lines, "SENDSPIN_RELAY_TOKEN")
        self.assertEqual(token, "explicit")

    def test_disable_clears_the_url_but_keeps_the_token(self) -> None:
        """Disable turns the relay off without discarding the paired token."""
        self._run("enable", "--url", "https://a.example.com", "--token", "kept")
        self._run("disable")
        content = self.env_file.read_text()
        self.assertIn("SENDSPIN_RELAY_URL=\n", content)
        self.assertIn("SENDSPIN_RELAY_TOKEN=kept", content)

    def test_status_reports_disabled_with_no_file(self) -> None:
        """Status against a nonexistent env file reports disabled, no token."""
        output = self._run("status")
        self.assertIn("disabled", output)
        self.assertIn("(not set)", output)

    def test_status_masks_the_token(self) -> None:
        """Status never prints the full token."""
        self._run("enable", "--url", "https://a.example.com", "--token", "0123456789ab")
        output = self._run("status")
        self.assertIn("enabled, https://a.example.com", output)
        self.assertIn("012345…", output)
        self.assertNotIn("0123456789ab", output)

    def test_restart_is_invoked_unless_no_restart(self) -> None:
        """Without --no-restart, the command restarts the systemd unit."""
        url = "https://a.example.com"
        argv = ["enable", "--url", url, "--env-file", str(self.env_file)]
        with patch("sendspin_service.relay_cli._restart_service") as restart:
            relay_cli.main(argv)
        restart.assert_called_once()


class ServerDispatchTests(unittest.TestCase):
    """Confirm 'relay' argv routes to relay_cli without starting the server."""

    def test_relay_subcommand_never_constructs_the_service(self) -> None:
        """server.main(["relay", ...]) never touches the running service."""
        directory = self.enterContext(TemporaryDirectory())
        env_file = Path(directory) / "sendspin-service"
        with (
            patch("sendspin_service.server.SendspinService") as service,
            patch("sendspin_service.server.SendSpinServer") as sendspin,
        ):
            exit_code = server.main(["relay", "status", "--env-file", str(env_file)])
        self.assertEqual(exit_code, 0)
        service.assert_not_called()
        sendspin.assert_not_called()
