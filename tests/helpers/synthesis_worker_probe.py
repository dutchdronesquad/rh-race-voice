"""Controlled subprocess for supervisor failure and isolation tests."""

from __future__ import annotations

import json
import os
import sys
import time
import wave
from pathlib import Path


def main() -> None:
    """Simulate blocking/crashing inference through the real pipe protocol."""
    for count, line in enumerate(sys.stdin, start=1):
        data = json.loads(line)
        if data.get("crash"):
            raise SystemExit(3)
        time.sleep(data.get("delay", 0))
        reply = {"ok": True, "result": {"pid": os.getpid(), "count": count}}
        if data.get("mock_audio") and data.get("operation") == "synthesize":
            root = Path(sys.argv[1])
            path = root / "probe.wav"
            with wave.open(str(path), "wb") as wav:
                wav.setparams((1, 2, 8000, 0, "NONE", "not compressed"))
                wav.writeframes(count.to_bytes(2, "little") * 80)
            signatures = [
                (stat.st_ino, stat.st_size, stat.st_mtime_ns)
                for suffix in (".onnx", ".onnx.json")
                for stat in ((root / "models" / f"{data['model']}{suffix}").stat(),)
            ]
            reply["result"].update(
                path=str(path),
                cache_hit=False,
                duration_ms=0,
                model_signature=signatures,
            )
        sys.stdout.write(json.dumps(reply) + "\n")
        sys.stdout.flush()


if __name__ == "__main__":
    main()
