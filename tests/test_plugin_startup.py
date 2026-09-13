"""Exercise startup and audio-check behavior without running RotorHazard."""

# ruff: noqa: PT009, SLF001

from __future__ import annotations

import importlib
import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from types import ModuleType
from unittest.mock import Mock, patch


def _load_plugin() -> ModuleType:
    """Stub RH and synthesis dependencies while loading the real plugin and UI."""
    package = ModuleType("race_voice_startup_test")
    package.__path__ = [
        str(Path(__file__).resolve().parents[1] / "custom_plugins/race_voice")
    ]
    with patch.dict(
        sys.modules,
        {
            package.__name__: package,
            f"{package.__name__}.piper": Mock(),
            "eventmanager": Mock(),
            "filtermanager": Mock(),
            "flask": Mock(),
            "RHUI": Mock(),
        },
    ):
        return importlib.import_module(f"{package.__name__}.plugin")


plugin_module = _load_plugin()


class PluginStartupTests(unittest.TestCase):
    """Verify automatic checks without gating audio or blocking RH startup."""

    def setUp(self) -> None:
        """Record RH registrations and defer executor work until the test runs it."""
        self.rhapi = Mock()
        self.rhapi.server.data_dir = self.enterContext(TemporaryDirectory())
        with (
            patch.object(plugin_module, "AudioQueue"),
            patch.object(plugin_module, "ThreadPoolExecutor"),
            patch.object(plugin_module, "SendspinServiceClient"),
        ):
            self.plugin = plugin_module.RaceVoicePlugin(self.rhapi)

    def test_startup_event_schedules_background_check(self) -> None:
        """Wait for RH startup and dispatch the check instead of performing I/O."""
        self.plugin._sendspin.health.assert_not_called()
        self.plugin._synth_pool.submit.assert_not_called()
        registrations = [
            call
            for call in self.rhapi.events.on.call_args_list
            if call.args[0] == plugin_module.Evt.STARTUP
        ]
        self.assertEqual(len(registrations), 1)
        registrations[0].args[1]({})
        self.plugin._synth_pool.submit.assert_called_once_with(
            self.plugin._check_sendspin_service
        )
        self.plugin._sendspin.health.assert_not_called()

    def test_audio_check_queues_without_health_gate(self) -> None:
        """A service check failure must not disable a later manual playback test."""
        self.plugin._sendspin.health.side_effect = OSError("service starting")
        with self.assertLogs(plugin_module.logger, level="WARNING"):
            self.assertFalse(self.plugin._check_sendspin_service())
        self.plugin._sendspin.health.reset_mock()
        self.plugin.play_audio_check()
        self.plugin._synth_pool.submit.assert_called_once_with(
            self.plugin._play_audio_check
        )
        self.plugin._synth_pool.submit.call_args.args[0]()
        self.plugin._sendspin.health.assert_not_called()
        self.plugin._audio_queue.enqueue.assert_called_once()

    def test_successful_startup_check_only_logs(self) -> None:
        """Avoid a success notification on each RH restart."""
        self.plugin._sendspin.health.return_value = {
            "ok": True,
            "status": "ok",
            "version": "1.1.0",
            "aiosendspin_version": "9.1.1",
        }
        with self.assertLogs(plugin_module.logger, level="INFO"):
            self.assertTrue(self.plugin._check_sendspin_service())
        self.rhapi.ui.message_notify.assert_not_called()
        self.rhapi.ui.message_alert.assert_not_called()

    def test_unhealthy_service_is_logged(self) -> None:
        """Retain an actionable warning when no browser is connected at startup."""
        self.plugin._sendspin.health.return_value = {"ok": False, "status": "error"}
        with self.assertLogs(plugin_module.logger, level="WARNING"):
            self.assertFalse(self.plugin._check_sendspin_service())
        self.rhapi.ui.message_alert.assert_called_once()

    def test_ui_keeps_audio_check_without_separate_service_button(self) -> None:
        """Keep the operator-facing audio test as the only check button."""
        names = [
            call.kwargs["name"]
            for call in self.rhapi.ui.register_quickbutton.call_args_list
        ]
        self.assertIn("race_voice_audio_check", names)
        self.assertNotIn("race_voice_service_check", names)
