"""Controlled subprocess for supervisor failure and isolation tests."""

from __future__ import annotations

import json
import os
import sys
import time
import wave
from pathlib import Path


def _signature(root: Path, model: str) -> list[tuple[int, int, int]]:
    return [
        (stat.st_ino, stat.st_size, stat.st_mtime_ns)
        for suffix in (".onnx", ".onnx.json")
        for stat in ((root / "models" / f"{model}{suffix}").stat(),)
    ]


def main() -> None:
    """Simulate blocking/crashing inference through the real pipe protocol."""
    warm_model = None
    for count, line in enumerate(sys.stdin, start=1):
        data = json.loads(line)
        if data.get("crash"):
            raise SystemExit(3)
        time.sleep(data.get("delay", 0))
        reply = {"ok": True, "result": {"pid": os.getpid(), "count": count}}
        if data.get("mock_audio") and data.get("operation") == "warmup":
            warm_model = data["model"]
            reply["result"].update(
                ready=True,
                model_ready=True,
                model_signature=_signature(Path(sys.argv[1]), warm_model),
            )
        if data.get("mock_audio") and data.get("operation") == "synthesize":
            root = Path(sys.argv[1])
            path = root / f"probe-{count}.wav"
            with wave.open(str(path), "wb") as wav:
                rate = data.get("sample_rate", 8000)
                wav.setparams((1, 2, rate, 0, "NONE", "not compressed"))
                marker = data.get("pcm_marker", count)
                frames = data.get("frames", 80)
                wav.writeframes(marker.to_bytes(2, "little") * frames)
            reply["result"].update(
                path=str(path),
                cache_hit=False,
                duration_ms=0,
                model_signature=_signature(root, data["model"]),
                model_ready=warm_model == data["model"],
            )
        sys.stdout.write(json.dumps(reply) + "\n")
        sys.stdout.flush()


if __name__ == "__main__":
    main()
