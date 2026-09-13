"""Verify manual preparation preserves existing cache files."""

# ruff: noqa: PT009, SLF001

from __future__ import annotations

import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import Mock

from tests.test_plugin_startup import plugin_module


class PrecachePreparationTests(unittest.TestCase):
    """Prepare twice without clearing or replacing reusable audio."""

    def test_preparation_reuses_files_and_fills_missing_segments(self) -> None:
        """The manager delegates validation to synthesis without deleting files."""
        root = Path(self.enterContext(TemporaryDirectory()))
        existing = root / "precache/laps/existing.wav"
        existing.parent.mkdir(parents=True)
        existing.write_bytes(b"existing audio")
        tts = Mock()
        tts.precache_dir_for_model.return_value = root / "precache"
        synthesized = []

        def synthesize(**kwargs):  # noqa: ANN003, ANN202
            path = root / kwargs["subdir"] / (kwargs["text"] + ".wav")
            hit = path.exists()
            if not hit:
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(b"generated")
                synthesized.append(path)
            return SimpleNamespace(cache_hit=hit)

        tts.synthesize_to_cache.side_effect = synthesize
        executor = Mock()
        laps = Mock()
        laps.precache_lap_segments.return_value = [
            SimpleNamespace(text="existing", subdir="precache/laps"),
            SimpleNamespace(text="missing", subdir="precache/laps"),
        ]
        clocks = Mock()
        clocks.precache_phrases.return_value = []
        manager = plugin_module.PrecacheManager(
            tts=tts,
            lap_callouts=laps,
            synth_pool=executor,
            prepare_model=Mock(),
            clock_callouts=clocks,
            schedule_phrase=lambda n, _: str(n),
            pilot_names_for_heat=Mock(),
            heat_name_for_id=Mock(),
            notify=Mock(),
        )
        settings = SimpleNamespace(model_name="en", params=Mock())
        for _ in range(2):
            manager.rebuild(settings, None)
            self.assertEqual(existing.read_bytes(), b"existing audio")
            callback, *args = executor.submit.call_args.args
            callback(*args)
        self.assertEqual(existing.read_bytes(), b"existing audio")
        self.assertEqual(len(synthesized), len(set(synthesized)))
        self.assertIn(root / "precache/laps/missing.wav", synthesized)

    def test_cancelled_preparation_does_not_generate_phrases(self) -> None:
        """A cancelled pending preparation leaves phrase synthesis untouched."""
        manager = plugin_module.PrecacheManager(
            tts=Mock(),
            lap_callouts=Mock(),
            synth_pool=Mock(),
            prepare_model=Mock(),
            clock_callouts=Mock(),
            schedule_phrase=Mock(),
            pilot_names_for_heat=Mock(),
            heat_name_for_id=Mock(),
            notify=Mock(),
        )
        manager.rebuild(SimpleNamespace(model_name="en"), None)
        callback, *args = manager._synth_pool.submit.call_args.args
        manager.cancel()
        callback(*args)
        manager._tts.synthesize_to_cache.assert_not_called()
