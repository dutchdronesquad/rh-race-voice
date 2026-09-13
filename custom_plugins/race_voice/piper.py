"""Piper TTS model loading, synthesis, and WAV cache handling."""

from __future__ import annotations

import hashlib
import io
import json
import logging
import os
import tempfile
import threading
import time
import unicodedata
import urllib.error
import urllib.request
import wave
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

import onnxruntime
from piper import PiperVoice
from piper.config import PiperConfig, SynthesisConfig

from .const import VOICE_MODELS

if TYPE_CHECKING:
    from collections.abc import Callable

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class SynthesisParams:
    """Synthesis parameters that control Piper output and the WAV cache key."""

    speed: str
    noise: str
    noise_w: str


@dataclass(frozen=True)
class SynthesisResult:
    """Result metadata for a synthesized or cached WAV file."""

    text: str
    wav_path: Path
    duration_ms: int
    cache_hit: bool


class PiperSynthesizer:
    """Generate local WAV files with Piper and cache them by phrase/speed.

    WAV files are stored under ``tts_dir/{model_name}/{sha1}_{speed}.wav``
    so that switching voice models never causes stale-cache collisions.
    """

    def __init__(
        self,
        model_dir: Path,
        tts_dir: Path,
        set_status: Callable[[str], None],
    ) -> None:
        """Initialize model and TTS cache directories."""
        self._model_dir = model_dir
        self._tts_dir = tts_dir
        self._set_status = set_status
        self._voice: Any | None = None
        self._loaded_model: str | None = None
        self._voice_lock = threading.Lock()
        self._cache_locks: dict[tuple[str, str, str], threading.Lock] = {}
        self._cache_locks_lock = threading.Lock()

        self._model_dir.mkdir(parents=True, exist_ok=True)
        self._tts_dir.mkdir(parents=True, exist_ok=True)

    def synthesize_to_cache(
        self,
        text: str,
        model_name: str,
        params: SynthesisParams,
        *,
        subdir: str = "",
    ) -> SynthesisResult | None:
        """Return a cached WAV for text, synthesizing it with Piper if needed.

        When *subdir* is given the WAV is written to
        ``{tts_dir}/{model_name}/{subdir}/`` instead of directly under the
        model directory.
        """
        normalized_text = self.normalize_text(text)
        if not normalized_text:
            self._set_status("No text supplied")
            return None

        cache_key = self.cache_key(normalized_text, params)
        model_tts_dir = self._tts_dir / model_name
        if subdir:
            model_tts_dir = model_tts_dir / subdir
        model_tts_dir.mkdir(parents=True, exist_ok=True)
        wav_path = model_tts_dir / f"{cache_key}.wav"
        started = time.perf_counter()

        cache_lock = self._cache_lock_for(model_name, subdir, cache_key)
        with cache_lock:
            if wav_path.exists() and self.valid_wav(wav_path):
                duration_ms = int((time.perf_counter() - started) * 1000)
                logger.debug(
                    "Race Voice cache hit for '%s': %s", normalized_text, wav_path
                )
                return SynthesisResult(
                    text=normalized_text,
                    wav_path=wav_path,
                    duration_ms=duration_ms,
                    cache_hit=True,
                )

            voice = self._load_voice(model_name)
            if voice is None:
                return None

            tmp_path: Path | None = None
            try:
                buf = io.BytesIO()
                with wave.open(buf, "wb") as wav_file:
                    voice.synthesize_wav(
                        normalized_text,
                        wav_file,
                        syn_config=self._make_syn_config(params),
                    )
                tmp_path = self._temp_wav_path(model_tts_dir)
                _prepend_silence(buf, tmp_path)
                tmp_path.replace(wav_path)
            except Exception as exc:
                if tmp_path is not None:
                    tmp_path.unlink(missing_ok=True)
                wav_path.unlink(missing_ok=True)
                self._set_status(f"Synthesis failed: {exc}")
                logger.exception(
                    "Race Voice synthesis failed for '%s'", normalized_text
                )
                return None

        duration_ms = int((time.perf_counter() - started) * 1000)
        return SynthesisResult(
            text=normalized_text,
            wav_path=wav_path,
            duration_ms=duration_ms,
            cache_hit=False,
        )

    def tts_dir_for_model(self, model_name: str) -> Path:
        """Return the cache subdirectory for a specific model."""
        return self._tts_dir / model_name

    def tmp_dir_for_model(self, model_name: str) -> Path:
        """Return the ephemeral tmp subdirectory for a specific model."""
        return self._tts_dir / model_name / "tmp"

    def test_dir_for_model(self, model_name: str) -> Path:
        """Return the test-phrase subdirectory for a specific model."""
        return self._tts_dir / model_name / "test"

    def precache_dir_for_model(self, model_name: str) -> Path:
        """Return the pre-generated lap phrase subdirectory for a specific model."""
        return self._tts_dir / model_name / "precache"

    def cache_status_text(self) -> str:
        """Return cache directory and total file-count status."""
        cached_files = sum(1 for path in self._tts_dir.rglob("*.wav") if path.is_file())
        return f"{self._tts_dir} | {cached_files} cached WAV files"

    def prepare_model(self, model_name: str, params: SynthesisParams) -> bool:
        """Load the model and verify synthesis with the current parameters."""
        voice = self._load_voice(model_name)
        if voice is None:
            return False
        try:
            buf = io.BytesIO()
            with wave.open(buf, "wb") as wav_file:
                voice.synthesize_wav(
                    "ready", wav_file, syn_config=self._make_syn_config(params)
                )
        except Exception:
            logger.exception("Race Voice: model preparation failed for %s", model_name)
            return False
        else:
            logger.info("Race Voice: model prepared for %s", model_name)
            return True

    def _load_voice(self, model_name: str) -> Any | None:
        """Load the selected Piper model once, downloading files if necessary."""
        if self._voice is not None and self._loaded_model == model_name:
            return self._voice

        with self._voice_lock:
            if self._voice is not None and self._loaded_model == model_name:
                return self._voice

            model_path = self._ensure_model_files(model_name)
            if model_path is None:
                return None

            try:
                self._set_status(f"Loading model {model_name}")
                config_path = model_path.with_suffix(".onnx.json")
                with config_path.open("r", encoding="utf-8") as f:
                    config_dict = json.load(f)
                sess_options = onnxruntime.SessionOptions()
                sess_options.intra_op_num_threads = max(
                    1, min(2, (os.cpu_count() or 2) - 1)
                )
                self._voice = PiperVoice(
                    config=PiperConfig.from_dict(config_dict),
                    session=onnxruntime.InferenceSession(
                        str(model_path),
                        sess_options=sess_options,
                        providers=["CPUExecutionProvider"],
                    ),
                )
                self._loaded_model = model_name
                self._set_status(f"Model loaded: {model_name}")
            except Exception as exc:
                self._voice = None
                self._loaded_model = None
                self._set_status(f"Model load failed: {exc}")
                logger.exception("Race Voice failed to load Piper model %s", model_name)
                return None

            return self._voice

    def _cache_lock_for(
        self, model_name: str, subdir: str, cache_key: str
    ) -> threading.Lock:
        """Return the in-process lock for one final WAV cache path."""
        lock_key = (model_name, subdir, cache_key)
        with self._cache_locks_lock:
            lock = self._cache_locks.get(lock_key)
            if lock is None:
                lock = threading.Lock()
                self._cache_locks[lock_key] = lock
            return lock

    @staticmethod
    def _temp_wav_path(directory: Path) -> Path:
        """Create a unique temporary WAV path in the destination directory."""
        with tempfile.NamedTemporaryFile(
            prefix=".race_voice_",
            suffix=".wav.tmp",
            dir=directory,
            delete=False,
        ) as tmp_file:
            return Path(tmp_file.name)

    def _ensure_model_files(self, model_name: str) -> Path | None:
        """Ensure the selected Piper model and JSON config are present locally."""
        model = VOICE_MODELS.get(model_name)
        if model is None:
            self._set_status(f"Unknown model: {model_name}")
            return None

        model_path = self._model_dir / f"{model_name}.onnx"
        config_path = self._model_dir / f"{model_name}.onnx.json"
        if model_path.exists() and config_path.exists():
            return model_path

        self._set_status(f"Downloading model {model_name}")
        base_url = model["base_url"]
        downloads = (
            (f"{base_url}.onnx", model_path),
            (f"{base_url}.onnx.json", config_path),
        )
        for url, destination in downloads:
            if destination.exists():
                continue
            try:
                self._download_file(url, destination)
            except (OSError, urllib.error.URLError) as exc:
                destination.unlink(missing_ok=True)
                self._set_status(f"Model download failed: {exc}")
                logger.exception("Race Voice model download failed from %s", url)
                return None

        return model_path

    @staticmethod
    def _download_file(url: str, destination: Path) -> None:
        """Download a URL to a local file with a temporary partial file."""
        partial_path = destination.with_suffix(f"{destination.suffix}.part")
        with (
            urllib.request.urlopen(url, timeout=60) as response,  # noqa: S310
            partial_path.open("wb") as output_file,
        ):
            while True:
                chunk = response.read(1024 * 1024)
                if not chunk:
                    break
                output_file.write(chunk)
        partial_path.replace(destination)

    @staticmethod
    def _make_syn_config(params: SynthesisParams) -> SynthesisConfig:
        return SynthesisConfig(
            length_scale=1.0 / float(params.speed),
            noise_scale=float(params.noise),
            noise_w_scale=float(params.noise_w),
        )

    @staticmethod
    def normalize_text(text: str) -> str:
        """Normalize text before synthesis and cache-key generation."""
        normalized = unicodedata.normalize("NFKC", text).strip()
        return " ".join(normalized.split())

    @staticmethod
    def cache_key(text: str, params: SynthesisParams) -> str:
        """Build a SHA1-based WAV cache key from text and synthesis params."""
        digest = hashlib.sha1(text.lower().encode("utf-8")).hexdigest()  # noqa: S324
        return f"{digest}_{params.speed}_{params.noise}_{params.noise_w}"

    @staticmethod
    def valid_wav(path: Path) -> bool:
        """Return whether a cached path can be opened as a WAV file."""
        try:
            with wave.open(str(path), "rb") as wav_file:
                return wav_file.getnframes() > 0
        except (OSError, wave.Error):
            return False


_LEADING_SILENCE_MS = 80


def _prepend_silence(src: io.BytesIO, dest: Path) -> None:
    """Write *src* WAV to *dest* with a short silence prepended.

    Piper occasionally produces a weak or clipped opening frame.  A brief
    run of zero-valued samples before the speech gives the decoder and the
    playback pipeline enough headroom to start cleanly.
    """
    src.seek(0)
    with wave.open(src, "rb") as r:
        params = r.getparams()
        pcm = r.readframes(r.getnframes())

    silence_frames = int(params.framerate * _LEADING_SILENCE_MS / 1000)
    silence = b"\x00" * silence_frames * params.nchannels * params.sampwidth

    with wave.open(str(dest), "wb") as w:
        w.setparams(params)
        w.writeframes(silence + pcm)
