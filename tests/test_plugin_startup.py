"""Exercise startup and audio-check behavior without running RotorHazard."""

# ruff: noqa: PT009, SLF001

from __future__ import annotations

import importlib
import sys
import threading
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
        self.options = {}
        self.rhapi.db.option.side_effect = lambda name, default: self.options.get(  # noqa: PLW0108
            name, default
        )
        self.rhapi.server.data_dir = self.enterContext(TemporaryDirectory())
        with (
            patch.object(plugin_module, "AudioQueue", side_effect=lambda **_: Mock()),
            patch.object(
                plugin_module, "ThreadPoolExecutor", side_effect=lambda **_: Mock()
            ),
            patch.object(
                plugin_module, "SendspinServiceClient", side_effect=lambda **_: Mock()
            ),
        ):
            self.plugin = plugin_module.RaceVoicePlugin(self.rhapi)
        self.plugin._audio_queue.clear.return_value = 0
        self.plugin._cloud_audio_queue.clear.return_value = 0

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

    def test_enabled_startup_does_not_prepare_cache(self) -> None:
        """Cache preparation remains an explicit operator action."""
        self.options[plugin_module.ENABLE_OPTION] = True
        self.plugin._precache = Mock()
        self.plugin._on_startup()
        self.plugin._precache.rebuild.assert_not_called()

    def test_audio_check_queues_without_health_gate(self) -> None:
        """A service check failure must not disable a later manual playback test."""
        self.plugin._sendspin.health.side_effect = OSError("service starting")
        with self.assertLogs(plugin_module.logger, level="WARNING"):
            self.assertFalse(self.plugin._check_sendspin_service())
        self.plugin._sendspin.health.reset_mock()
        self.plugin.play_audio_check()
        self.plugin._synth_pool.submit.assert_not_called()
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

    def test_audio_is_copied_to_cloud_with_timing_and_priority(self) -> None:
        """Send the same clips and schedule to both queues without resynthesis."""
        self.options[plugin_module.SENDSPIN_CLOUD_URL_OPTION] = "https://cloud.example"
        paths = [Path("tone.wav")]
        self.plugin._enqueue_audio(
            "tone", paths, plugin_module.Priority.HIGH, 3.0, 123.0, 0.4
        )
        for queue in (self.plugin._audio_queue, self.plugin._cloud_audio_queue):
            queue.enqueue.assert_called_once()
            job = queue.enqueue.call_args.kwargs
            self.assertIs(job["wav_paths"], paths)
            self.assertEqual(job["play_at"], 123.0)
            self.assertEqual(job["priority"], plugin_module.Priority.HIGH)
            self.assertEqual(job["volume"], 0.4)
            self.assertGreater(job["expiry_sec"], 2.5)
            self.assertLessEqual(job["expiry_sec"], 3.0)

    def test_empty_or_duplicate_cloud_url_only_queues_once(self) -> None:
        """Default local setup and duplicate URLs must not produce double audio."""
        for url in ("", " http://127.0.0.1:8766/ "):
            self.options[plugin_module.SENDSPIN_CLOUD_URL_OPTION] = url
            self.plugin._enqueue_audio("test", [Path("test.wav")])
        self.assertEqual(self.plugin._audio_queue.enqueue.call_count, 2)
        self.plugin._cloud_audio_queue.enqueue.assert_not_called()

    def test_stop_clears_both_queues_and_dispatches_both_services(self) -> None:
        """Neither service's stop request waits for the other's network response."""
        self.options[plugin_module.SENDSPIN_CLOUD_URL_OPTION] = "https://cloud.example"
        self.plugin._audio_queue.clear.return_value = 1
        self.plugin._cloud_audio_queue.clear.return_value = 2
        self.plugin.stop_audio()
        self.plugin._audio_queue.clear.assert_called_once()
        self.plugin._cloud_audio_queue.clear.assert_called_once()
        self.plugin._audio_queue.stop.assert_called_once_with(
            self.plugin._sendspin.stop
        )
        self.plugin._cloud_audio_queue.stop.assert_called_once_with(
            self.plugin._cloud_sendspin.stop
        )

    def test_synthesis_finishing_after_stop_cannot_requeue(self) -> None:
        """Stop while synthesis is running and discard the resulting audio."""
        generation = self.plugin._generation

        def synthesize(*_args):  # noqa: ANN002, ANN202
            self.plugin.stop_audio()
            return Path("old.wav")

        with patch.object(self.plugin, "_synthesize", side_effect=synthesize):
            self.plugin._enqueue(
                "old",
                plugin_module.Priority.NORMAL,
                float("inf"),
                generation=generation,
            )
        self.plugin._audio_queue.enqueue.assert_not_called()
        self.plugin._enqueue_audio("new", [Path("new.wav")])
        self.plugin._audio_queue.enqueue.assert_called_once()

    def test_heat_change_skips_pending_old_synthesis(self) -> None:
        """Invalidate pending jobs without clearing reusable pre-cache files."""
        generation = self.plugin._generation
        with patch.object(self.plugin, "_clear_wavs") as clear:
            self.plugin._on_heat_set({})
        self.assertEqual(clear.call_count, 1)
        self.assertEqual(clear.call_args.args[1], "ephemeral")
        with patch.object(self.plugin, "_synthesize") as synthesize:
            self.plugin._enqueue(
                "old",
                plugin_module.Priority.NORMAL,
                float("inf"),
                generation=generation,
            )
        synthesize.assert_not_called()

    def test_stop_waits_for_active_upload_before_clearing_service(self) -> None:
        """Order an in-flight upload, stop, then fresh audio without blocking RH."""
        started = threading.Event()
        release = threading.Event()
        finished = threading.Event()
        calls = []

        def player(text, *_args):  # noqa: ANN001, ANN002, ANN202
            if text == "old":
                started.set()
                release.wait(2)
            calls.append(text)
            if text == "new":
                finished.set()

        queue = plugin_module.AudioQueue(player)
        try:
            queue.enqueue("old", [Path("old.wav")])
            self.assertTrue(started.wait(1))
            queue.stop(lambda: calls.append("stop"))
            queue.enqueue("new", [Path("new.wav")])
            release.set()
            self.assertTrue(finished.wait(2))
            self.assertEqual(calls, ["old", "stop", "new"])
        finally:
            release.set()

    def test_blocked_cloud_does_not_delay_local_audio(self) -> None:
        """A second local callout plays while the first cloud upload is blocked."""
        self.options[plugin_module.SENDSPIN_CLOUD_URL_OPTION] = "https://cloud.example"
        cloud_started = threading.Event()
        release_cloud = threading.Event()
        local_finished = threading.Event()
        local_calls = []

        def local_player(*args):  # noqa: ANN002, ANN202
            local_calls.append(args[0])
            if len(local_calls) == 2:
                local_finished.set()

        def cloud_player(*args):  # noqa: ANN002, ANN202
            cloud_started.set()
            release_cloud.wait(5)
            raise OSError("cloud unavailable")

        self.plugin._audio_queue = plugin_module.AudioQueue(local_player)
        self.plugin._cloud_audio_queue = plugin_module.AudioQueue(cloud_player)
        with self.assertLogs("race_voice_startup_test.audio_queue", level="ERROR"):
            try:
                self.plugin._enqueue_audio("first", [Path("test.wav")])
                self.assertTrue(cloud_started.wait(2))
                self.plugin._enqueue_audio("second", [Path("test.wav")])
                self.assertTrue(local_finished.wait(2))
                self.assertEqual(local_calls, ["first", "second"])
            finally:
                release_cloud.set()
                self.plugin._audio_queue._queue.join()
                self.plugin._cloud_audio_queue._queue.join()

    def test_spoken_race_countdown_has_signal_priority(self) -> None:
        """Remaining-time voice announcements can interrupt ordinary speech."""
        self.options[plugin_module.ENABLE_OPTION] = True
        for seconds in (60, 30, 10):
            with self.subTest(seconds=seconds):
                self.plugin._on_clock_callout({"seconds_remaining": seconds})
                job = self.plugin._synth_pool.submit.call_args
                with patch.object(
                    self.plugin, "_synthesize", return_value=Path("clock.wav")
                ):
                    job.args[0](*job.args[1:], **job.kwargs)
                self.assertEqual(
                    self.plugin._audio_queue.enqueue.call_args.kwargs["priority"],
                    plugin_module.Priority.SIGNAL,
                )

    def test_spoken_scheduled_start_has_signal_priority(self) -> None:
        """A spoken pre-start countdown has the same priority as race tones."""
        self.plugin._enqueue_schedule_callout(
            "Race begins in 5 seconds", self.plugin._settings()
        )
        job = self.plugin._synth_pool.submit.call_args
        with patch.object(
            self.plugin, "_synthesize", return_value=Path("schedule.wav")
        ):
            job.args[0](*job.args[1:], **job.kwargs)
        self.assertEqual(
            self.plugin._audio_queue.enqueue.call_args.kwargs["priority"],
            plugin_module.Priority.SIGNAL,
        )
