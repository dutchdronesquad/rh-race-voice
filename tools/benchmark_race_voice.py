"""Compare real lap preparation with disk reuse and bounded RAM reuse.

Run with ``python -m tools.benchmark_race_voice --model-dir <existing models>``.
Only temporary caches are written; no models are downloaded or audio played.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import platform
import statistics
import sys
import time
from importlib.metadata import version
from pathlib import Path
from tempfile import TemporaryDirectory

from custom_plugins.race_voice.const import VOICE_MODELS
from sendspin_service.race.race_protocol import Context, EventKind, RaceEvent
from sendspin_service.race.speech import SpeechEngine
from sendspin_service.synthesis.synthesis import SynthesisWorker


class MeasuredWorker(SynthesisWorker):
    """Count subprocess exchanges rather than claiming CPU savings from latency."""

    def __init__(self, root: Path, *, max_cache_bytes: int) -> None:
        """Use the production worker with only its RAM budget varied."""
        super().__init__(root, max_cache_bytes=max_cache_bytes)
        self.worker_jobs = 0

    async def _execute(self, request: bytes) -> dict:
        self.worker_jobs += 1
        return await super()._execute(request)


def _summary(values: list[float]) -> dict:
    ordered = sorted(values)
    return {
        "samples": len(values),
        "p50_ms": round(statistics.median(values), 3),
        "p95_ms": round(ordered[min(len(values) - 1, int(len(values) * 0.95))], 3),
    }


async def _measure(args: argparse.Namespace, *, max_cache_bytes: int) -> dict:
    settings = {"model": args.model, "speed": 1, "noise": 0.667, "noise_w": 0.8}
    with TemporaryDirectory(prefix="race-voice-benchmark-") as directory:
        root = Path(directory)
        (root / "models").mkdir()
        for suffix in (".onnx", ".onnx.json"):
            name = args.model + suffix
            (root / "models" / name).symlink_to(args.model_dir / name)
        worker = MeasuredWorker(root, max_cache_bytes=max_cache_bytes)
        speech = SpeechEngine(worker)

        async def reusable(text: str, subdir: str) -> dict:
            return await worker.request(
                {**settings, "operation": "synthesize", "text": text, "subdir": subdir},
                deadline=time.monotonic() + 120,
            )

        lap_ms = []
        cached_pair_ms = []

        try:
            started = time.perf_counter()
            await reusable("Klaas,", "precache/pilots")
            cold_model_ms = (time.perf_counter() - started) * 1000
            lap_phrase = speech.segments(_event(0), args.model)[1].text
            await reusable(lap_phrase, "precache/laps")
            before_laps = worker.worker_jobs
            for index in range(args.laps):
                started = time.perf_counter()
                audio = await speech.synthesize(
                    _event(index),
                    settings,
                    deadline=time.monotonic() + 120,
                    priority=3,
                    is_current=lambda: True,
                )
                finished = time.perf_counter()
                if len(audio) != 3:
                    raise RuntimeError("Benchmark did not produce a complete lap")
                lap_ms.append((finished - started) * 1000)
            lap_jobs = worker.worker_jobs - before_laps
            before_reuse = worker.worker_jobs
            for _ in range(args.cached_rounds):
                started = time.perf_counter()
                await asyncio.gather(
                    reusable("Klaas,", "precache/pilots"),
                    reusable(lap_phrase, "precache/laps"),
                )
                cached_pair_ms.append((time.perf_counter() - started) * 1000)
            reuse_jobs = worker.worker_jobs - before_reuse
        finally:
            await worker.close()
    return {
        "ram_cache_bytes": max_cache_bytes,
        "cold_first_name_ms": round(cold_model_ms, 3),
        "uncached_lap_preparation": _summary(lap_ms),
        "uncached_lap_worker_jobs": lap_jobs,
        "cached_name_and_lap_pair": _summary(cached_pair_ms),
        "cached_pair_worker_jobs": reuse_jobs,
    }


def _event(index: int) -> RaceEvent:
    now = time.monotonic()
    return RaceEvent(
        session_id="benchmark",
        event_id=f"benchmark:{index}",
        sequence=index,
        context=Context("benchmark", 1, 0, 1),
        kind=EventKind.LAP,
        occurred_at=now,
        expires_at=now + 120,
        play_at=None,
        pilot_id=1,
        text=f" {23 + index // 10}.{index % 10}",
        lap=3,
        pilot_name="Klaas",
        asset=None,
        winner=False,
    )


async def _compare(args: argparse.Namespace) -> dict:
    return {
        "environment": {
            "architecture": platform.machine(),
            "logical_cpus": os.cpu_count(),
            "python": platform.python_version(),
            "piper_tts": version("piper-tts"),
            "onnxruntime": version("onnxruntime"),
        },
        "model": args.model,
        "scope": "Speech preparation only; excludes HTTP, playback and player latency",
        "disk": await _measure(args, max_cache_bytes=0),
        "ram": await _measure(args, max_cache_bytes=8 * 1024 * 1024),
        "cached_restart": await _restart_measure(args),
    }


async def _restart_measure(args: argparse.Namespace) -> dict:
    """Compare a fresh child with existing WAVs, before/after explicit model warmup."""
    settings = {"model": args.model, "speed": 1, "noise": 0.667, "noise_w": 0.8}
    results = {}
    with TemporaryDirectory(prefix="race-voice-restart-") as directory:
        root = Path(directory)
        (root / "models").mkdir()
        for suffix in (".onnx", ".onnx.json"):
            name = args.model + suffix
            (root / "models" / name).symlink_to(args.model_dir / name)
        for index, mode in enumerate(("seed", "cached_wavs_only", "model_warmed")):
            worker = SynthesisWorker(root)
            speech = SpeechEngine(worker)
            lap_phrase = speech.segments(_event(0), args.model)[1].text
            try:
                started = time.perf_counter()
                if mode == "model_warmed":
                    result = await worker.request(
                        {**settings, "operation": "warmup"},
                        deadline=time.monotonic() + 120,
                    )
                    if not result["ready"]:
                        raise RuntimeError("Benchmark warmup failed")
                for text, subdir in (
                    ("Klaas,", "precache/pilots"),
                    (lap_phrase, "precache/laps"),
                ):
                    await worker.request(
                        {
                            **settings,
                            "operation": "synthesize",
                            "text": text,
                            "subdir": subdir,
                        },
                        deadline=time.monotonic() + 120,
                    )
                prepared_ms = (time.perf_counter() - started) * 1000
                if mode == "seed":
                    continue
                started = time.perf_counter()
                audio = await speech.synthesize(
                    _event(index),
                    settings,
                    deadline=time.monotonic() + 120,
                    priority=3,
                    is_current=lambda: True,
                )
                if len(audio) != 3:
                    raise RuntimeError(
                        "Restart benchmark did not produce a complete lap"
                    )
                results[mode] = {
                    "setup_ms": round(prepared_ms, 3),
                    "first_lap_preparation_ms": round(
                        (time.perf_counter() - started) * 1000, 3
                    ),
                }
            finally:
                await worker.close()
    return results


def main() -> None:
    """Require existing model files so the comparison never depends on downloads."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model-dir", type=Path, required=True)
    parser.add_argument("--model", choices=VOICE_MODELS, default="nl_NL-pim-medium")
    parser.add_argument("--laps", type=int, default=12)
    parser.add_argument("--cached-rounds", type=int, default=30)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    args.model_dir = args.model_dir.resolve()
    if not (1 <= args.laps <= 1000 and 1 <= args.cached_rounds <= 1000):
        parser.error("Sample counts must be between 1 and 1000")
    for suffix in (".onnx", ".onnx.json"):
        if not (args.model_dir / (args.model + suffix)).is_file():
            parser.error(f"Missing model file: {args.model + suffix}")
    result = json.dumps(asyncio.run(_compare(args)), indent=2) + "\n"
    if args.output:
        args.output.write_text(result, encoding="utf-8")
    sys.stdout.write(result)


if __name__ == "__main__":
    main()
