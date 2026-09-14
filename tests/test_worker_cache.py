"""Exercise shared Piper cache behaviour with a deterministic fake voice."""

# ruff: noqa: PT009, PT027, SLF001

from __future__ import annotations

import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import Mock, patch

from sendspin_service.synthesis.synthesis import SynthesisWorker, WorkerUnavailableError
from sendspin_service.synthesis.synthesis_worker import (
    WorkerSynthesizer,
    execute,
    validate_request,
)


class WorkerCacheTests(unittest.TestCase):
    """Protect model revision keys, file isolation and reusable prepared audio."""

    def setUp(self) -> None:
        """Use real WAV/cache writes without requiring a downloaded ONNX model."""
        self.root = Path(self.enterContext(TemporaryDirectory()))
        self.tts = WorkerSynthesizer(self.root)
        model = self.root / "models/test.onnx"
        model.write_bytes(b"model revision one")
        model.with_suffix(".onnx.json").write_text("{}")
        self.model = model
        self.enterContext(
            patch.object(self.tts, "_ensure_model_files", return_value=model)
        )
        self.voice = Mock()

        def synthesize(_text, wav, **_kwargs):  # noqa: ANN001, ANN003, ANN202
            wav.setparams((1, 2, 22050, 0, "NONE", "not compressed"))
            wav.writeframes(bytes(2205 * 2))

        self.voice.synthesize_wav.side_effect = synthesize
        self.enterContext(
            patch.object(self.tts, "_load_voice", return_value=self.voice)
        )
        self.request = {
            "operation": "synthesize",
            "model": "nl_NL-pim-medium",
            "text": "Klaas,",
            "speed": 1,
            "noise": 0.667,
            "noise_w": 0.8,
            "subdir": "precache/pilots",
        }

    def test_valid_audio_is_reused_without_another_inference(self) -> None:
        """Repeated preparation must preserve valid cached files."""
        first = execute(self.tts, self.request)
        audio = Path(first["path"]).read_bytes()
        second = execute(self.tts, self.request)
        self.assertTrue(second["cache_hit"])
        self.assertEqual(Path(second["path"]).read_bytes(), audio)
        self.voice.synthesize_wav.assert_called_once()

    def test_model_content_case_and_settings_change_the_cache_identity(self) -> None:
        """Changed voices or pronunciation must not reuse an incompatible WAV."""
        paths = {execute(self.tts, self.request)["path"]}
        paths.add(execute(self.tts, self.request | {"text": "KLAAS,"})["path"])
        paths.add(execute(self.tts, self.request | {"speed": 1.5})["path"])
        self.model.write_bytes(b"different model revision with another length")
        paths.add(execute(self.tts, self.request)["path"])
        self.assertEqual(len(paths), 4)

    def test_clear_does_not_invalidate_already_pinned_audio(self) -> None:
        """Playback holds immutable bytes rather than a deletable cache pathname."""
        first = execute(self.tts, self.request)
        supervisor = SynthesisWorker(self.root)
        audio = supervisor._pin_audio(first["path"])
        execute(self.tts, self.request | {"operation": "clear", "subdir": ""})
        self.assertFalse(Path(first["path"]).exists())
        self.assertGreater(len(audio), 44)
        self.assertEqual(audio[:4], b"RIFF")

    def test_temporary_cleanup_preserves_prepared_audio_and_models(self) -> None:
        """Heat cleanup only removes dynamic lap files, even with a warm cache."""
        prepared = execute(self.tts, self.request)
        temporary = execute(self.tts, self.request | {"subdir": "tmp"})
        result = execute(
            self.tts, self.request | {"operation": "clear", "subdir": "tmp"}
        )
        self.assertEqual(result, {"cleared": 1})
        self.assertFalse(Path(temporary["path"]).exists())
        self.assertTrue(Path(prepared["path"]).exists())
        self.assertTrue(self.model.exists())

    def test_paths_and_invalid_tuning_are_rejected(self) -> None:
        """Reject filesystem paths and nonfinite synthesis settings."""
        for changes in (
            {"model": "../../outside"},
            {"subdir": "../../outside"},
            {"speed": 0},
            {"noise": float("nan")},
            {"noise_w": True},
        ):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                validate_request(self.request | changes)
        with self.assertRaises(WorkerUnavailableError):
            SynthesisWorker(self.root)._pin_audio("/etc/hosts")


if __name__ == "__main__":
    unittest.main()
