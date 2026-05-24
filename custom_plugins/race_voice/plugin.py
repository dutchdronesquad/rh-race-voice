"""RotorHazard integration for the Race Voice plugin."""

from __future__ import annotations

import contextlib
import json
import logging
import os
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from eventmanager import Evt
from filtermanager import Flt

from .audio_queue import AudioQueue, Priority
from .const import (
    DEFAULT_MODEL,
    DEFAULT_NOISE_SCALE,
    DEFAULT_NOISE_W_SCALE,
    DEFAULT_SENDSPIN_SERVICE_TIMEOUT,
    DEFAULT_SENDSPIN_SERVICE_URL,
    DEFAULT_SPEED,
    DEFAULT_TEST_PHRASE,
    ENABLE_OPTION,
    NOISE_SCALE_OPTION,
    NOISE_W_SCALE_OPTION,
    SENDSPIN_SERVICE_TIMEOUT_OPTION,
    SENDSPIN_SERVICE_URL_OPTION,
    SPEECH_SPEED_OPTION,
    TEST_PHRASE_OPTION,
    VOICE_MODEL_OPTION,
    VOICE_MODELS,
)
from .output import SendspinServiceClient
from .piper import PiperSynthesizer, SynthesisParams, SynthesisResult
from .services.lap_callouts import LapCalloutSegments
from .services.precache import PrecacheManager
from .services.schedule import ScheduleCalloutManager
from .ui import register_ui

logger = logging.getLogger(__name__)

_ASSET_DIR = Path(__file__).parent / "assets"
_AUDIO_CHECK_WAV = _ASSET_DIR / "moavii-foreign.wav"
_STAGE_BEEP_WAV = _ASSET_DIR / "stage.wav"
_BUZZER_WAV = _ASSET_DIR / "buzzer.wav"

# Status messages that are surfaced to the UI as notifications.
_UI_NOTIFY_PREFIXES = ("Downloading model",)
_DEBUG_STATUS_PREFIXES = ("Loading model", "Model loaded")

_LAP_CALLOUT_EXPIRY_SEC = 10.0
_STAGE_TONE_STALE_AFTER_SEC = 1.0
_START_BUZZER_STALE_AFTER_SEC = 1.0

try:
    with (Path(__file__).parent / "locales.json").open(encoding="utf-8") as _f:
        _LOCALES: dict[str, dict] = json.load(_f)
except (OSError, json.JSONDecodeError) as exc:
    raise RuntimeError("Race Voice: failed to load locales.json") from exc


@dataclass(frozen=True)
class VoiceSettings:
    """TTS settings snapshot used by background synthesis jobs."""

    model_name: str
    params: SynthesisParams


class RaceVoicePlugin:
    """RotorHazard plugin for Piper TTS generation and WAV caching."""

    def __init__(self, rhapi: Any) -> None:
        """Initialize the plugin and register RotorHazard integration points."""
        self._rhapi = rhapi
        data_dir = Path(
            getattr(self._rhapi.server, "data_dir", Path.home() / "rh-data")
        )
        cache_root = data_dir / "race_voice_cache"
        self._tts = PiperSynthesizer(
            model_dir=cache_root / "models",
            tts_dir=cache_root / "tts",
            set_status=self._set_status,
        )
        self._sendspin = SendspinServiceClient(
            service_url=self._sendspin_service_url,
            timeout_s=self._sendspin_service_timeout,
        )
        self._audio_queue = AudioQueue(player=self._sendspin.play)
        self._prepared_settings: VoiceSettings | None = None
        self._synth_pool = ThreadPoolExecutor(
            max_workers=os.cpu_count() or 4,
            thread_name_prefix="race_voice_synth",
        )
        self._schedule_callouts = ScheduleCalloutManager(
            enqueue_callout=self._enqueue_schedule_callout,
            phrase_for=self._schedule_phrase_for_settings,
        )
        self._lap_callouts = LapCalloutSegments(locale_for_model=self._locale_for_model)
        self._precache = PrecacheManager(
            tts=self._tts,
            lap_callouts=self._lap_callouts,
            synth_pool=self._synth_pool,
            prepare_model=self._prepare_model,
            schedule_phrase=self._schedule_phrase,
            pilot_names_for_heat=self._pilot_names_for_heat,
            heat_name_for_id=self._heat_name_for_id,
            notify=self._rhapi.ui.message_notify,
        )

        register_ui(
            self._rhapi,
            test_callback=self.generate_test_phrase,
            audio_check_callback=self.play_audio_check,
            stop_audio_callback=self.stop_audio,
            clear_cache_callback=self.clear_tts_cache,
            rebuild_precache_callback=self.rebuild_precache,
        )
        self._register_events()
        self._register_filters()
        logger.info("Race Voice plugin initialized")

    # ------------------------------------------------------------------
    # Event + filter registration
    # ------------------------------------------------------------------

    def _register_events(self) -> None:
        self._rhapi.events.on(
            Evt.HEAT_SET, self._on_heat_set, name="race_voice_heat_set"
        )
        self._rhapi.events.on(
            Evt.DATABASE_RESET,
            self._on_event_cache_reset,
            name="race_voice_database_reset",
        )
        self._rhapi.events.on(
            Evt.RACE_STAGE_TONE,
            self._on_stage_tone,
            name="local_voice_stage_tone",
        )
        self._rhapi.events.on(
            Evt.RACE_START, self._on_race_start, name="local_voice_race_start"
        )
        self._rhapi.events.on(
            Evt.RACE_SCHEDULE,
            self._on_race_schedule,
            name="race_voice_race_schedule",
        )
        self._rhapi.events.on(
            Evt.RACE_SCHEDULE_CANCEL,
            self._on_race_schedule_cancel,
            name="race_voice_race_schedule_cancel",
        )

    def _register_filters(self) -> None:
        self._rhapi.filters.add(
            Flt.EMIT_PHONETIC_DATA,
            "race_voice_phonetic_data",
            self._on_phonetic_data,
        )
        self._rhapi.filters.add(
            Flt.EMIT_PHONETIC_TEXT,
            "race_voice_phonetic_text",
            self._on_phonetic_text,
        )

    # ------------------------------------------------------------------
    # Filter hooks
    # ------------------------------------------------------------------

    def _on_phonetic_data(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Snapshot lap data and schedule synthesis off the RH event thread."""
        if not self._enabled():
            return payload
        lap_number: int = payload.get("lap", 0)
        if lap_number == 0:
            return payload  # holeshot - not announced
        snapshot = {
            "lap": lap_number,
            "pilot": payload.get("pilot"),
            "callsign": payload.get("callsign"),
            "phonetic": payload.get("phonetic"),
            "expires_at": time.monotonic() + _LAP_CALLOUT_EXPIRY_SEC,
            "settings": self._settings(),
        }
        self._synth_pool.submit(self._synthesize_lap, snapshot)
        return payload

    def _synthesize_lap(self, snapshot: dict[str, Any]) -> None:
        """Synthesize lap callout in background using reusable segments."""
        expires_at: float = snapshot["expires_at"]
        if time.monotonic() > expires_at:
            logger.info("Race Voice dropped expired lap synthesis job")
            return

        settings = snapshot["settings"]
        callout = self._lap_callouts.plan(snapshot, settings.model_name)

        wav_paths = [
            path
            for segment in callout.segments
            if (path := self._synthesize(segment.text, segment.subdir, settings))
        ]

        if wav_paths:
            self._audio_queue.enqueue(
                text=callout.label,
                wav_paths=wav_paths,
                priority=Priority.NORMAL,
                expiry_sec=max(0.0, expires_at - time.monotonic()),
            )

    def _on_phonetic_text(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Snapshot phonetic text and schedule synthesis off the RH event thread."""
        if not self._enabled():
            return payload
        text = payload.get("text") if isinstance(payload, dict) else None
        if not isinstance(text, str) or not text.strip():
            return payload
        priority = Priority.HIGH if payload.get("winner_flag") else Priority.NORMAL
        self._synth_pool.submit(
            self._enqueue,
            text.strip(),
            priority,
            time.monotonic() + 5.0,
            "",
            self._settings(),
        )
        return payload

    # ------------------------------------------------------------------
    # Event handlers
    # ------------------------------------------------------------------

    def _on_heat_set(self, _args: dict[str, Any]) -> None:
        """Wipe ephemeral lap-time WAVs and queued audio when a new heat is selected."""
        dropped = self._audio_queue.clear()
        if dropped:
            logger.info("Race Voice cleared %d queued audio jobs on heat set", dropped)

        settings = self._settings()
        model_name = settings.model_name
        self._clear_wavs(self._tts.tmp_dir_for_model(model_name), "ephemeral")

    def _current_heat_id(self) -> int | None:
        heat_id = self._rhapi.race.heat
        return heat_id or None

    def _on_race_start(self, _args: dict[str, Any]) -> None:
        """Play the start buzzer when the race begins."""
        if not self._enabled():
            return
        play_at = _float_or_none(getattr(self._rhapi.race, "start_time_internal", None))
        self._audio_queue.enqueue(
            text="race start",
            wav_paths=[_BUZZER_WAV],
            priority=Priority.HIGH,
            expiry_sec=_scheduled_expiry_sec(play_at, _START_BUZZER_STALE_AFTER_SEC),
            play_at=play_at,
        )

    def _on_stage_tone(self, args: dict[str, Any]) -> None:
        """Play the staging beep for this arm tone."""
        if not self._enabled():
            return
        play_at = _float_or_none(args.get("scheduled_at_monotonic"))
        self._audio_queue.enqueue(
            text="arm tone",
            wav_paths=[_STAGE_BEEP_WAV],
            priority=Priority.HIGH,
            expiry_sec=_scheduled_expiry_sec(play_at, _STAGE_TONE_STALE_AFTER_SEC),
            play_at=play_at,
        )

    def _on_event_cache_reset(self, _args: dict[str, Any]) -> None:
        """Wipe event-specific WAVs when RotorHazard starts a new data set."""
        self._precache.cancel()
        self._schedule_callouts.cancel()
        settings = self._settings()
        model_name = settings.model_name
        self._clear_wavs(self._tts.tmp_dir_for_model(model_name), "ephemeral")
        self._clear_wavs(self._tts.precache_dir_for_model(model_name), "pre-cache")

    def _on_race_schedule(self, args: dict[str, Any]) -> None:
        """Schedule countdown callouts when a race start is deferred."""
        if not self._enabled():
            self._schedule_callouts.cancel()
            return
        scheduled_at = args.get("scheduled_at")
        if scheduled_at is None:
            return
        self._schedule_callouts.schedule(scheduled_at, self._settings())

    def _on_race_schedule_cancel(self, _args: dict[str, Any]) -> None:
        """Cancel pending countdown callouts when a scheduled race is cancelled."""
        self._schedule_callouts.cancel()

    def _enqueue_schedule_callout(self, phrase: str, settings: VoiceSettings) -> None:
        """Enqueue a race schedule callout from a background timer."""
        self._synth_pool.submit(
            self._enqueue,
            phrase,
            Priority.HIGH,
            time.monotonic() + 8.0,
            "precache/schedule",
            settings,
        )

    # ------------------------------------------------------------------
    # UI button handlers
    # ------------------------------------------------------------------

    def generate_test_phrase(self, _args: dict[str, Any] | None = None) -> None:
        """Generate the configured test phrase and report the result."""
        text = str(
            self._option(TEST_PHRASE_OPTION, default=DEFAULT_TEST_PHRASE)
        ).strip()
        if not text:
            text = DEFAULT_TEST_PHRASE
        wav_path = self._synthesize(text, "test")
        if wav_path is None:
            self._rhapi.ui.message_alert("Race Voice test failed - check logs")
            return
        self._audio_queue.enqueue(
            text=text, wav_paths=[wav_path], priority=Priority.HIGH
        )
        self._rhapi.ui.message_notify(f"Race Voice test phrase queued: {wav_path.name}")

    def play_audio_check(self, _args: dict[str, Any] | None = None) -> None:
        """Play the bundled audio-check WAV through Sendspin."""
        if not _AUDIO_CHECK_WAV.exists():
            self._rhapi.ui.message_alert("Race Voice audio check WAV is missing")
            return
        self._audio_queue.enqueue(
            text="Sendspin audio check",
            wav_paths=[_AUDIO_CHECK_WAV],
            priority=Priority.HIGH,
            expiry_sec=45.0,
        )
        self._rhapi.ui.message_notify(
            f"Race Voice audio check queued: {_AUDIO_CHECK_WAV.name}"
        )

    def stop_audio(self, _args: dict[str, Any] | None = None) -> None:
        """Stop current Sendspin playback and clear queued audio."""
        dropped = self._audio_queue.clear()
        self._sendspin.stop()
        self._rhapi.ui.message_notify(f"Race Voice audio stopped ({dropped} queued)")

    def clear_tts_cache(self, _args: dict[str, Any] | None = None) -> None:
        """Delete all cached WAV files for the currently selected model."""
        self._precache.cancel()

        model_name = self._model_name()
        model_tts_dir = self._tts.tts_dir_for_model(model_name)
        if not model_tts_dir.exists():
            self._rhapi.ui.message_notify("Race Voice: cache is already empty")
            return
        wav_files = [path for path in model_tts_dir.rglob("*.wav") if path.is_file()]
        for wav_file in wav_files:
            wav_file.unlink(missing_ok=True)
        self._rhapi.ui.message_notify(
            f"Race Voice: cleared {len(wav_files)} WAV files for {model_name}"
        )

    def rebuild_precache(self, _args: dict[str, Any] | None = None) -> None:
        """Clear and regenerate pre-cached phrases for the current model and heat."""
        self._precache.rebuild(self._settings(), self._current_heat_id())

    # ------------------------------------------------------------------
    # Synthesis helpers
    # ------------------------------------------------------------------

    def _synthesize(
        self,
        text: str,
        subdir: str = "",
        settings: VoiceSettings | None = None,
    ) -> Path | None:
        """Synthesize text with the current model and params, return WAV path."""
        if settings is None:
            settings = self._settings()
        result = self._tts.synthesize_to_cache(
            text=text,
            model_name=settings.model_name,
            params=settings.params,
            subdir=subdir,
        )
        if result is None:
            return None
        self._record_generation(result)
        return result.wav_path

    def _enqueue(
        self,
        text: str,
        priority: Priority,
        expires_at: float,
        subdir: str = "",
        settings: VoiceSettings | None = None,
    ) -> None:
        """Synthesize text and push it onto the audio queue."""
        if time.monotonic() > expires_at:
            logger.info("Race Voice dropped expired enqueue job: '%s'", text)
            return
        if wav_path := self._synthesize(text, subdir, settings):
            self._audio_queue.enqueue(
                text=text,
                wav_paths=[wav_path],
                priority=priority,
                expiry_sec=max(0.0, expires_at - time.monotonic()),
            )

    def _record_generation(self, result: SynthesisResult) -> None:
        source = "cache hit" if result.cache_hit else "synthesized"
        size = result.wav_path.stat().st_size if result.wav_path.exists() else 0
        dur = result.duration_ms
        self._set_status(
            f"Ready ({source}): {result.wav_path.name} | {size}B | {dur}ms"
        )

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _prepare_model(self, settings: VoiceSettings | None = None) -> None:
        if settings is None:
            settings = self._settings()
        if settings == self._prepared_settings:
            return
        if self._tts.prepare_model(settings.model_name, settings.params):
            self._prepared_settings = settings

    def _pilot_names_for_heat(self, heat_id: int) -> list[str]:
        slots = self._rhapi.db.slots_by_heat(heat_id)
        pilot_names: list[str] = []
        for slot in slots:
            if not slot.pilot_id:
                continue
            pilot = self._rhapi.db.pilot_by_id(slot.pilot_id)
            if pilot is None:
                continue
            name = pilot.phonetic or pilot.callsign
            if name:
                pilot_names.append(str(name))
        return pilot_names

    def _heat_name_for_id(self, heat_id: int) -> str | int:
        heat = self._rhapi.db.heat_by_id(heat_id)
        return heat.name if heat else heat_id

    def _schedule_phrase(self, threshold: int, model_name: str) -> str:
        locale = self._locale_for_model(model_name)
        return locale.get("race_schedule", {}).get(
            str(threshold), f"Race begins in {threshold} seconds"
        )

    def _schedule_phrase_for_settings(
        self, threshold: int, settings: VoiceSettings
    ) -> str:
        return self._schedule_phrase(threshold, settings.model_name)

    def _clear_wavs(self, directory: Path, label: str) -> None:
        """Delete WAV files under a cache subdirectory."""
        if not directory.exists():
            return
        count = sum(
            1
            for wav_file in directory.rglob("*.wav")
            if wav_file.unlink(missing_ok=True) is None
        )
        if count:
            logger.info("Race Voice cleared %d %s WAV files", count, label)

    def _set_status(self, status: str) -> None:
        if any(status.startswith(prefix) for prefix in _DEBUG_STATUS_PREFIXES):
            logger.debug("Race Voice status: %s", status)
        else:
            logger.info("Race Voice status: %s", status)
        if any(status.startswith(prefix) for prefix in _UI_NOTIFY_PREFIXES):
            with contextlib.suppress(Exception):
                self._rhapi.ui.message_notify(f"Race Voice: {status}")

    def _enabled(self) -> bool:
        return self._flag(ENABLE_OPTION, default=False)

    def _flag(self, option: str, *, default: bool = True) -> bool:
        value = self._option(option, default=default)
        if isinstance(value, bool):
            return value
        return str(value).lower() not in {"0", "false", "no", ""}

    def _locale(self) -> dict:
        return self._locale_for_model(self._model_name())

    @staticmethod
    def _locale_for_model(model_name: str) -> dict:
        return _LOCALES.get(model_name[:2], _LOCALES["en"])

    def _settings(self) -> VoiceSettings:
        return VoiceSettings(model_name=self._model_name(), params=self._synth_params())

    def _model_name(self) -> str:
        value = str(self._option(VOICE_MODEL_OPTION, default=DEFAULT_MODEL))
        return value if value in VOICE_MODELS else DEFAULT_MODEL

    def _synth_params(self) -> SynthesisParams:
        return SynthesisParams(
            speed=self._float_option(SPEECH_SPEED_OPTION, DEFAULT_SPEED),
            noise=self._float_option(NOISE_SCALE_OPTION, DEFAULT_NOISE_SCALE),
            noise_w=self._float_option(NOISE_W_SCALE_OPTION, DEFAULT_NOISE_W_SCALE),
        )

    def _float_option(self, option: str, default: str) -> str:
        value = self._option(option, default=default)
        try:
            return f"{float(value):.3f}"
        except (TypeError, ValueError):
            return f"{float(default):.3f}"

    def _sendspin_service_url(self) -> str:
        value = str(
            self._option(
                SENDSPIN_SERVICE_URL_OPTION,
                default=DEFAULT_SENDSPIN_SERVICE_URL,
            )
        ).strip()
        return value or DEFAULT_SENDSPIN_SERVICE_URL

    def _sendspin_service_timeout(self) -> float:
        value = self._option(
            SENDSPIN_SERVICE_TIMEOUT_OPTION,
            default=DEFAULT_SENDSPIN_SERVICE_TIMEOUT,
        )
        try:
            return max(0.2, float(value))
        except (TypeError, ValueError):
            return float(DEFAULT_SENDSPIN_SERVICE_TIMEOUT)

    def _option(self, name: str, *, default: Any) -> Any:
        value = self._rhapi.db.option(name, default=default)
        return default if value is False else value


def _float_or_none(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _scheduled_expiry_sec(play_at: float | None, stale_after_sec: float) -> float:
    if play_at is None:
        return stale_after_sec
    return max(stale_after_sec, play_at - time.monotonic() + stale_after_sec)
