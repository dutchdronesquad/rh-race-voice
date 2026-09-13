"""Private, line-delimited worker protocol; never exposed as a network API."""

from __future__ import annotations

import hashlib
import json
import logging
import math
import sys
from importlib.metadata import version
from pathlib import Path
from typing import TYPE_CHECKING, Any

from custom_plugins.race_voice.const import VOICE_MODELS
from custom_plugins.race_voice.piper import PiperSynthesizer, SynthesisParams

if TYPE_CHECKING:
    from collections.abc import Callable

logger = logging.getLogger(__name__)
MAX_REQUEST_BYTES = 32_768


class WorkerSynthesizer(PiperSynthesizer):
    """Reuse Piper in a dedicated process, with versioned content cache keys."""

    def __init__(self, root: Path) -> None:
        """Keep worker state entirely outside RotorHazard."""
        super().__init__(root / "models", root / "tts", logger.info)
        self._revision = ""
        self._model_revisions: dict[tuple, str] = {}

    def _run_native[T](
        self, function: Callable[..., T], *args: Any, **kwargs: Any
    ) -> T:
        """Already isolated from both RH and the service event loop."""
        return function(*args, **kwargs)

    def _cache_lock_for(self, model_name: str, subdir: str, cache_key: str):  # noqa: ANN202, ARG002
        # The worker is serial; keep one lock rather than one per dynamic lap phrase.
        return self._native_lock

    def select_revision(self, model_name: str) -> None:
        """Hash model/config once per file revision, outside the service loop."""
        model = self._ensure_model_files(model_name)
        if model is None:
            raise RuntimeError("Model is unavailable")
        paths = (model, model.with_suffix(".onnx.json"))
        signatures = tuple(
            (str(p), p.stat().st_ino, p.stat().st_size, p.stat().st_mtime_ns)
            for p in paths
        )
        revision = self._model_revisions.get(signatures)
        if revision is None:
            digest = hashlib.sha256(version("piper-tts").encode())
            for path in paths:
                with path.open("rb") as handle:
                    while chunk := handle.read(1024 * 1024):
                        digest.update(chunk)
            revision = digest.hexdigest()
            self._model_revisions = {signatures: revision}
        if self._revision != revision:
            self._voice = None
            self._loaded_model = None
        self._revision = revision

    def cache_key(self, text: str, params: SynthesisParams) -> str:
        """Keep case, synthesis tuning, engine version and model contents distinct."""
        content = json.dumps(
            [self._revision, text, params.speed, params.noise, params.noise_w],
            ensure_ascii=False,
        )
        return hashlib.sha256(content.encode()).hexdigest()


def validate_request(data: object) -> dict:
    """Validate bounded private requests before any file/model operation."""
    if not isinstance(data, dict):
        raise TypeError("Request must be an object")
    operation = data.get("operation")
    if operation not in ("synthesize", "warmup", "clear"):
        raise ValueError("Unknown worker operation")
    if data.get("model") not in VOICE_MODELS:
        raise ValueError("Unknown voice model")
    for key in ("speed", "noise", "noise_w"):
        value = data.get(key)
        if type(value) not in (int, float) or not math.isfinite(value):
            raise ValueError("Voice settings must be finite numbers")
        if not 0 <= value <= 10 or (key == "speed" and value < 0.1):
            raise ValueError("Voice setting out of range")
    if data.get("subdir", "") not in (
        "",
        "tmp",
        "test",
        "precache/pilots",
        "precache/laps",
        "precache/clock",
        "precache/schedule",
    ):
        raise ValueError("Unsupported cache category")
    text = data.get("text", "")
    if not isinstance(text, str) or len(text) > 4096:
        raise ValueError("Invalid synthesis text")
    if operation == "synthesize" and not text.strip():
        raise ValueError("Synthesis requires text")
    return data


def execute(tts: WorkerSynthesizer, data: dict) -> dict:
    """Complete one operation before reading the next request."""
    data = validate_request(data)
    model = data["model"]
    params = SynthesisParams(
        *(str(float(data[k])) for k in ("speed", "noise", "noise_w"))
    )
    if data["operation"] == "clear":
        count = 0
        for path in (tts._tts_dir / model).rglob("*.wav"):  # noqa: SLF001
            path.unlink(missing_ok=True)
            count += 1
        return {"cleared": count}
    tts.select_revision(model)
    if data["operation"] == "warmup":
        return {"ready": tts.prepare_model(model, params)}
    result = tts.synthesize_to_cache(
        data["text"], model, params, subdir=data.get("subdir", "")
    )
    if result is None:
        raise RuntimeError("Speech generation failed")
    return {
        "path": str(result.wav_path),
        "cache_hit": result.cache_hit,
        "duration_ms": result.duration_ms,
    }


def main() -> None:
    """Read trusted supervisor requests; logging and native diagnostics use stderr."""
    logging.basicConfig(level=logging.INFO, stream=sys.stderr)
    root = Path(sys.argv[1]).resolve()
    tts = WorkerSynthesizer(root)
    while line := sys.stdin.buffer.readline(MAX_REQUEST_BYTES + 1):
        if len(line) > MAX_REQUEST_BYTES or not line.endswith(b"\n"):
            raise SystemExit("Worker request exceeds limit")
        try:
            result = {"ok": True, "result": execute(tts, json.loads(line))}
        except Exception as err:
            logger.exception("Synthesis worker request failed")
            result = {"ok": False, "error": str(err)[:1024]}
        sys.stdout.write(json.dumps(result) + "\n")
        sys.stdout.flush()


if __name__ == "__main__":
    main()
