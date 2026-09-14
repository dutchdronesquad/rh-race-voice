"""Probe Piper's native boundary after the same monkey-patching RH performs."""

# Imports intentionally follow patch_all, in a dedicated subprocess.
# ruff: noqa: E402, SLF001, S101, PLR0915, T201

from gevent import monkey

monkey.patch_all()

import importlib
import json
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from tempfile import TemporaryDirectory
from types import ModuleType
from unittest.mock import Mock, patch

import gevent

_repo_root = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_repo_root))  # piper.py imports custom_plugins.race_voice.const

package = ModuleType("race_voice_gevent_probe")
package.__path__ = [str(_repo_root / "sendspin_service")]
sys.modules[package.__name__] = package
module = importlib.import_module(f"{package.__name__}.piper")
original_sleep = monkey.get_original("time", "sleep")
original_ident = monkey.get_original("_thread", "get_ident")
hub_ident = original_ident()


def main() -> None:
    """Keep a heartbeat running during synthesis, warmup and model loading."""
    active = 0
    max_concurrent = 0
    work_threads = []
    callback_threads = []
    ticks = []
    running = True
    fail = False

    def ticker() -> None:
        while running:
            if active:
                ticks.append(time.monotonic())
            gevent.sleep(0.005)

    def blocking_work(*_args, **_kwargs) -> None:  # noqa: ANN002, ANN003
        nonlocal active, max_concurrent
        active += 1
        max_concurrent = max(max_concurrent, active)
        work_threads.append(original_ident())
        try:
            original_sleep(0.12)
            if fail:
                raise RuntimeError("expected inference failure")
        finally:
            active -= 1

    def synthesize(_text, wav_file, **_kwargs) -> None:  # noqa: ANN001, ANN003
        wav_file.setparams((1, 2, 22050, 0, "NONE", "not compressed"))
        blocking_work()
        wav_file.writeframes(bytes(2205 * 2))

    def status(_text: str) -> None:
        callback_threads.append(original_ident())
        # RH notifications may yield; they must not run in a native worker/hub callback.
        gevent.sleep(0)

    heartbeat = gevent.spawn(ticker)
    with (
        TemporaryDirectory() as directory,
        ThreadPoolExecutor(max_workers=1) as executor,
    ):
        root = Path(directory)
        tts = module.PiperSynthesizer(root / "models", root / "tts", status)
        params = module.SynthesisParams("1.000", "0.667", "0.800")
        voice = Mock()
        voice.synthesize_wav.side_effect = synthesize
        tts._voice = voice
        tts._loaded_model = "en-test"
        # Control: the ordinary executor remains a greenlet under RH monkey-patching.
        executor.submit(blocking_work).result()
        baseline_ticks = len(ticks)
        assert work_threads.pop() == hub_ident

        before = len(ticks)
        result = executor.submit(
            tts.synthesize_to_cache, "pilot", "en-test", params
        ).result()
        synthesis_ticks = len(ticks) - before
        assert result is not None
        assert not result.cache_hit
        assert module.PiperSynthesizer.valid_wav(result.wav_path)
        data = result.wav_path.read_bytes()
        call_count = len(work_threads)
        cached = tts.synthesize_to_cache("pilot", "en-test", params)
        cache_reused = cached.cache_hit and len(work_threads) == call_count
        assert cached.wav_path.read_bytes() == data

        before = len(ticks)
        assert executor.submit(tts.prepare_model, "en-test", params).result()
        warmup_ticks = len(ticks) - before

        # Concurrent UI warmup and queued synthesis still serialize native work.
        jobs = [gevent.spawn(tts.prepare_model, "en-test", params) for _ in range(2)]
        gevent.joinall(jobs, raise_error=True)
        fail = True
        assert tts.synthesize_to_cache("failure", "en-test", params) is None
        fail = False
        error_recovered = (
            tts.synthesize_to_cache("recovery", "en-test", params) is not None
        )

        tts._voice = None
        model_path = root / "models/model.onnx"
        model_path.with_suffix(".onnx.json").write_text("{}")
        before = len(ticks)
        with (
            patch.object(tts, "_ensure_model_files", return_value=model_path),
            patch.object(module.PiperConfig, "from_dict", return_value=Mock()),
            patch.object(module, "PiperVoice", return_value=voice),
            patch.object(
                module.onnxruntime, "InferenceSession", side_effect=blocking_work
            ),
        ):
            assert executor.submit(tts.prepare_model, "en-test", params).result()
        load_ticks = len(ticks) - before
    running = False
    heartbeat.join()
    print(
        json.dumps(
            {
                "baseline_ticks": baseline_ticks,
                "synthesis_ticks": synthesis_ticks,
                "warmup_ticks": warmup_ticks,
                "load_ticks": load_ticks,
                "native_work": all(thread != hub_ident for thread in work_threads),
                "callbacks_on_hub": bool(callback_threads)
                and all(thread == hub_ident for thread in callback_threads),
                "cache_reused": cache_reused,
                "error_recovered": error_recovered,
                "max_concurrent": max_concurrent,
            }
        )
    )


if __name__ == "__main__":
    main()
