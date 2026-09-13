"""Standalone HTTP service for Sendspin playback."""

from __future__ import annotations

import argparse
import asyncio
import base64
import binascii
import contextlib
import hmac
import json
import logging
import os
import time
from dataclasses import dataclass
from importlib.metadata import version
from pathlib import Path
from typing import TYPE_CHECKING, Any
from urllib.parse import unquote

from aiohttp import BodyPartReader, web

from .audio_cache import AudioCache, MissingAudioError
from .audio_queue import DEFAULT_EXPIRY_SEC, AudioQueue, Priority, WavItem
from .player import add_player_routes
from .sendspin import SendSpinServer

if TYPE_CHECKING:
    from collections.abc import Sequence

logger = logging.getLogger(__name__)

SERVICE_VERSION = os.environ.get("SENDSPIN_SERVICE_VERSION", "0.0.0+dev")
DEFAULT_API_HOST = "127.0.0.1"
DEFAULT_API_PORT = 8766
DEFAULT_SENDSPIN_HOST = "0.0.0.0"  # noqa: S104
DEFAULT_SENDSPIN_PORT = 8927
DEFAULT_MAX_BODY_MB = 50
MAX_BODY_MB_LIMIT = 100
BYTES_PER_MIB = 1024 * 1024


@dataclass(frozen=True)
class ServiceConfig:
    """Runtime configuration for the Sendspin service."""

    api_host: str = DEFAULT_API_HOST
    api_port: int = DEFAULT_API_PORT
    sendspin_host: str = DEFAULT_SENDSPIN_HOST
    sendspin_port: int = DEFAULT_SENDSPIN_PORT
    advertise: bool = True
    max_body_bytes: int = DEFAULT_MAX_BODY_MB * BYTES_PER_MIB
    player_dir: Path | None = None
    api_token: str = ""
    race_cache_dir: Path | None = None


class SendspinService:
    """Own Sendspin playback and expose simple service operations."""

    def __init__(self, config: ServiceConfig) -> None:
        """Initialize the service backend."""
        self._config = config
        if (
            config.race_cache_dir is not None
            and config.api_host not in {"127.0.0.1", "::1", "localhost"}
            and not config.api_token
        ):
            raise ValueError("Race ingest on a network interface requires an API token")
        asset_dir = Path(__file__).parent / "assets"
        if not asset_dir.is_dir():
            asset_dir = (
                Path(__file__).resolve().parents[1] / "custom_plugins/race_voice/assets"
            )
        self._audio_cache = AudioCache(asset_dir)
        self._asset_dir = asset_dir
        self._sendspin = SendSpinServer(
            host=config.sendspin_host,
            port=config.sendspin_port,
            advertise=config.advertise,
        )
        self._queue = (
            AudioQueue(player=self._sendspin.play, interrupt=self._sendspin.stop)
            if config.race_cache_dir is None
            else None
        )

    def start(self) -> None:
        """Start the Sendspin server."""
        self._sendspin.start()

    def health(self) -> dict[str, Any]:
        """Return service health metadata."""
        connected_clients = self._sendspin.connected_client_count()
        return {
            "ok": True,
            "status": "ok",
            "version": SERVICE_VERSION,
            "aiosendspin_version": version("aiosendspin"),
            "api_host": self._config.api_host,
            "api_port": self._config.api_port,
            "sendspin_host": self._config.sendspin_host,
            "sendspin_port": self._config.sendspin_port,
            "connected_clients": connected_clients,
            "connected_players": connected_clients,
            "max_body_bytes": self._config.max_body_bytes,
            "api_auth_required": bool(self._config.api_token),
            "supports_multipart_play": self._queue is not None,
            "supports_audio_references": self._queue is not None,
            "bundled_audio": self._audio_cache.bundled_hashes,
            "race_event_preview": self._config.race_cache_dir is not None,
        }

    @property
    def max_body_bytes(self) -> int:
        """Return the configured maximum HTTP request body size."""
        return self._config.max_body_bytes

    @property
    def player_dir(self) -> Path | None:
        """Return the optional browser player build directory."""
        return self._config.player_dir

    @property
    def api_token(self) -> str:
        """Return the optional API bearer token."""
        return self._config.api_token

    def play(
        self, payload: dict[str, Any], wav_items: list[WavItem] | None = None
    ) -> dict[str, Any]:
        """Queue playback options with inline or separately uploaded WAV data."""
        if self._queue is None:
            raise web.HTTPConflict(reason="Race event mode owns playback; use v2")
        if wav_items is None:
            wav_items = _wav_items(payload)
        cached_audio = None
        if "wav_refs" in payload:
            wav_items, cached_audio = self._audio_cache.resolve(
                payload["wav_refs"], wav_items
            )
        if not wav_items:
            raise ValueError("wav_files must contain at least one WAV")
        priority = _priority(payload.get("priority"))
        if payload.get("kind") == "race_signal":
            priority = Priority.SIGNAL
        elif payload.get("kind") == "lap":
            priority = Priority.LAP
        expiry_sec = _expiry_sec(payload)
        play_at = _play_at(payload)
        volume = _volume(payload)
        text = str(payload.get("text") or "service audio")
        self._queue.enqueue(
            text=text,
            wav_items=wav_items,
            priority=priority,
            expiry_sec=expiry_sec,
            play_at=play_at,
            volume=volume,
        )
        result = {"queued": True, "count": len(wav_items)}
        if cached_audio is not None:
            result["cached_audio"] = cached_audio
        return result

    def stop(self) -> dict[str, Any]:
        """Stop active playback and clear queued jobs."""
        if self._queue is None:
            raise web.HTTPConflict(reason="Stop via a new v2 state generation")
        dropped = self._queue.clear()
        self._sendspin.stop()
        return {"stopped": True, "dropped": dropped}

    def shutdown(self) -> None:
        """Stop playback and close the underlying Sendspin server."""
        if self._queue is not None:
            self._queue.clear()
        self._sendspin.close()

    def add_race_routes(self, app: web.Application) -> None:
        """Load primary-only dependencies when explicitly enabled in a checkout."""
        if self._config.race_cache_dir is None:
            return
        from .race_ingest import RaceIngest, add_routes  # noqa: PLC0415
        from .race_planner import SendspinPlaybackSink  # noqa: PLC0415
        from .synthesis import SynthesisWorker  # noqa: PLC0415

        assets = {
            name: (self._asset_dir / filename).read_bytes()
            for name, filename in {
                "stage": "stage.wav",
                "buzzer": "buzzer.wav",
                "audio_check": "moavii-foreign.wav",
            }.items()
        }
        ingest = RaceIngest(
            SynthesisWorker(self._config.race_cache_dir),
            SendspinPlaybackSink(self._sendspin),
            assets,
        )
        add_routes(app, ingest)

        async def cleanup(_app: web.Application) -> None:
            await ingest.close()

        app.on_cleanup.append(cleanup)


def _create_app(service: SendspinService) -> web.Application:
    """Create the HTTP ingest application."""
    app = web.Application(
        client_max_size=service.max_body_bytes,
        middlewares=[_api_token_middleware],
    )
    app["service"] = service
    app.router.add_get("/health", _health)
    app.router.add_post("/v1/play", _play)
    app.router.add_post("/v1/stop", _stop)
    service.add_race_routes(app)
    add_player_routes(app, service.player_dir)
    return app


async def _health(request: web.Request) -> web.Response:
    """Return service health metadata."""
    return web.json_response(_service(request).health())


async def _play(request: web.Request) -> web.Response:
    """Queue playback from JSON or streaming multipart uploads."""
    started = time.monotonic()
    logger.info(
        "Sendspin service received %s: content_length=%s",
        request.path,
        request.content_length,
    )
    try:
        if request.content_type == "multipart/form-data":
            payload, wav_items = await _read_multipart_play(request)
            result = await asyncio.to_thread(_service(request).play, payload, wav_items)
        else:
            payload = await _read_json(request)
            result = await asyncio.to_thread(_service(request).play, payload)
        return web.json_response(result, status=202)
    except web.HTTPConflict as exc:
        return web.json_response({"error": exc.reason}, status=409)
    except web.HTTPRequestEntityTooLarge:
        return web.json_response({"error": "request body too large"}, status=413)
    except MissingAudioError as exc:
        return web.json_response({"missing_audio": exc.hashes}, status=409)
    except (TypeError, ValueError) as exc:
        return web.json_response({"error": str(exc)}, status=400)
    except Exception:
        logger.exception("Sendspin service request failed: %s", request.path)
        return web.json_response({"error": "internal server error"}, status=500)
    finally:
        elapsed = time.monotonic() - started
        if elapsed >= 1.0:
            logger.warning(
                "Sendspin service slow request: %s took %.3fs",
                request.path,
                elapsed,
            )


async def _stop(request: web.Request) -> web.Response:
    """Stop active playback and clear queued audio."""
    try:
        result = await asyncio.to_thread(_service(request).stop)
        return web.json_response(result)
    except web.HTTPConflict as exc:
        return web.json_response({"error": exc.reason}, status=409)
    except Exception:
        logger.exception("Sendspin service stop request failed")
        return web.json_response({"error": "internal server error"}, status=500)


@web.middleware
async def _api_token_middleware(
    request: web.Request,
    handler: web.RequestHandler,
) -> web.StreamResponse:
    if request.path in {"/v1/play", "/v1/stop"} or request.path.startswith("/v2/"):
        _require_api_token(request)
    return await handler(request)


def _require_api_token(request: web.Request) -> None:
    token = _service(request).api_token
    if not token:
        return
    scheme, _, actual_token = (
        request.headers.get("Authorization", "").strip().partition(" ")
    )
    if scheme.lower() == "bearer" and hmac.compare_digest(actual_token.strip(), token):
        return
    raise web.HTTPUnauthorized(
        text=json.dumps({"error": "missing or invalid API token"}),
        content_type="application/json",
        headers={"WWW-Authenticate": "Bearer"},
    )


async def _read_json(request: web.Request) -> dict[str, Any]:
    content_length = request.content_length
    if content_length == 0:
        return {}
    if content_length is not None and content_length > _service(request).max_body_bytes:
        raise web.HTTPRequestEntityTooLarge(
            max_size=_service(request).max_body_bytes,
            actual_size=content_length,
        )
    try:
        body = await request.read()
        payload = await asyncio.to_thread(json.loads, body)
    except json.JSONDecodeError as exc:
        raise ValueError("invalid JSON body") from exc
    if not isinstance(payload, dict):
        raise TypeError("JSON body must be an object")
    return payload


async def _read_multipart_play(
    request: web.Request,
) -> tuple[dict[str, Any], list[WavItem]]:
    """Read metadata and raw WAV parts without base64 or a large JSON document."""
    limit = _service(request).max_body_bytes
    if request.content_length is not None and request.content_length > limit:
        raise web.HTTPRequestEntityTooLarge(
            max_size=limit, actual_size=request.content_length
        )
    reader = await request.multipart()
    payload: dict[str, Any] | None = None
    items: list[WavItem] = []
    total = 0
    async for part in reader:
        if not isinstance(part, BodyPartReader):
            raise TypeError("nested multipart bodies are not supported")
        if part.name not in {"metadata", "wav_files"}:
            raise ValueError("unexpected multipart field")
        data = await _read_upload_part(part, limit - total)
        total += len(data)
        if part.name == "metadata":
            if payload is not None:
                raise ValueError("duplicate metadata field")
            payload = await asyncio.to_thread(json.loads, data)
            if not isinstance(payload, dict):
                raise TypeError("metadata must be a JSON object")
        else:
            items.append(WavItem(name=unquote(part.filename or "audio.wav"), data=data))
    if payload is None:
        raise ValueError("missing metadata field")
    return payload, items


async def _read_upload_part(part: BodyPartReader, limit: int) -> bytes:
    data = bytearray()
    while chunk := await part.read_chunk(size=64 * 1024):
        data.extend(chunk)
        if len(data) > limit:
            raise web.HTTPRequestEntityTooLarge(max_size=limit, actual_size=len(data))
    return bytes(data)


def _service(request: web.Request) -> SendspinService:
    return request.app["service"]


def _wav_items(payload: dict[str, Any]) -> list[WavItem]:
    if "wav_paths" in payload:
        raise ValueError("wav_paths are not supported; use wav_files")
    return _inline_wav_items(payload.get("wav_files"))


def _inline_wav_items(value: object) -> list[WavItem]:
    if not isinstance(value, list):
        return []
    items: list[WavItem] = []
    for index, item in enumerate(value, start=1):
        if not isinstance(item, dict):
            raise TypeError("wav_files entries must be objects")
        name = str(item.get("name") or f"inline-{index}.wav")
        data = item.get("data")
        encoding = str(item.get("encoding") or "base64").lower()
        if not isinstance(data, str):
            raise TypeError("wav_files entries must include string data")
        if encoding != "base64":
            raise ValueError("wav_files entries only support base64 encoding")
        try:
            wav_data = base64.b64decode(data, validate=True)
        except (binascii.Error, ValueError) as exc:
            message = "wav_files entries must contain valid base64 data"
            raise ValueError(message) from exc
        items.append(WavItem(name=name, data=wav_data))
    return items


def _priority(value: object) -> Priority:
    if isinstance(value, int):
        with contextlib.suppress(ValueError):
            return Priority(value)
    if isinstance(value, str):
        with contextlib.suppress(KeyError):
            return Priority[value.upper()]
    return Priority.NORMAL


def _play_at(payload: dict[str, Any]) -> float | None:
    """Return a local monotonic playback target from absolute or relative input."""
    if "play_at_delay_sec" in payload:
        with contextlib.suppress(TypeError, ValueError):
            delay_sec = float(payload.get("play_at_delay_sec"))
            return time.monotonic() + max(0.0, delay_sec)
        return time.monotonic()
    if "play_at" in payload:
        with contextlib.suppress(TypeError, ValueError):
            return float(payload.get("play_at"))
        return 0.0
    return None


def _expiry_sec(payload: dict[str, Any]) -> float:
    """Return playback expiry seconds from the request payload."""
    with contextlib.suppress(TypeError, ValueError):
        return float(payload.get("expiry_sec"))
    return DEFAULT_EXPIRY_SEC


def _volume(payload: dict[str, Any]) -> float:
    """Return clamped playback volume from the request payload."""
    with contextlib.suppress(TypeError, ValueError):
        volume = float(payload.get("volume"))
        return max(0.0, min(1.0, volume))
    return 1.0


def _env_str(name: str, default: str) -> str:
    value = os.environ.get(name)
    if value is None or not value.strip():
        return default
    return value.strip()


def _env_int(name: str, default: int) -> int:
    value = os.environ.get(name)
    if value is None:
        return default
    with contextlib.suppress(ValueError):
        return int(value)
    logger.warning("Ignoring invalid integer value for %s: %r", name, value)
    return default


def _env_bool(name: str, *, default: bool) -> bool:
    value = os.environ.get(name)
    if value is None:
        return default
    return value.strip().lower() not in {"0", "false", "no", "off"}


def _env_path(name: str) -> Path | None:
    value = os.environ.get(name)
    if value is None or not value.strip():
        return None
    return Path(value.strip()).expanduser()


def _body_limit_mb(value: int) -> int:
    if value < 1:
        logger.warning("Clamping SENDSPIN_MAX_BODY_MB to 1 MiB")
        return 1
    if value > MAX_BODY_MB_LIMIT:
        logger.warning("Clamping SENDSPIN_MAX_BODY_MB to %d MiB", MAX_BODY_MB_LIMIT)
        return MAX_BODY_MB_LIMIT
    return value


def _parse_args(argv: Sequence[str] | None = None) -> ServiceConfig:
    parser = argparse.ArgumentParser(description="Run the Sendspin playback service")
    parser.add_argument(
        "--host",
        default=_env_str("SENDSPIN_INGEST_HOST", DEFAULT_API_HOST),
        help="HTTP API host",
    )
    parser.add_argument(
        "--port",
        default=_env_int("SENDSPIN_INGEST_PORT", DEFAULT_API_PORT),
        type=int,
        help="HTTP API port",
    )
    parser.add_argument(
        "--sendspin-host",
        default=_env_str("SENDSPIN_HOST", DEFAULT_SENDSPIN_HOST),
        help="Sendspin WebSocket host",
    )
    parser.add_argument(
        "--sendspin-port",
        default=_env_int("SENDSPIN_PORT", DEFAULT_SENDSPIN_PORT),
        type=int,
        help="Sendspin WebSocket port",
    )
    parser.add_argument(
        "--no-advertise",
        action="store_false",
        default=_env_bool("SENDSPIN_ADVERTISE", default=True),
        dest="advertise",
        help="Disable Sendspin address advertising",
    )
    parser.add_argument(
        "--max-body-mb",
        default=_env_int("SENDSPIN_MAX_BODY_MB", DEFAULT_MAX_BODY_MB),
        type=int,
        help="Maximum JSON request body size in MiB",
    )
    parser.add_argument(
        "--player-dir",
        default=_env_path("SENDSPIN_PLAYER_DIR"),
        type=Path,
        help="Optional browser player build directory",
    )
    parser.add_argument(
        "--api-token",
        default=_env_str("SENDSPIN_API_TOKEN", ""),
        help="Bearer token for playback and race ingest endpoints",
    )
    parser.add_argument(
        "--experimental-race-cache-dir",
        type=Path,
        dest="race_cache_dir",
        help="Enable the local race-event preview using this Piper cache directory",
    )
    args = parser.parse_args(argv)
    max_body_mb = _body_limit_mb(args.max_body_mb)
    return ServiceConfig(
        api_host=args.host,
        api_port=args.port,
        sendspin_host=args.sendspin_host,
        sendspin_port=args.sendspin_port,
        advertise=args.advertise,
        max_body_bytes=max_body_mb * BYTES_PER_MIB,
        player_dir=args.player_dir,
        api_token=args.api_token.strip(),
        race_cache_dir=args.race_cache_dir,
    )


def main(argv: Sequence[str] | None = None) -> int:
    """Run the Sendspin service until interrupted."""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    config = _parse_args(argv)
    logger.info(
        "Sendspin service request body limit: %d MiB",
        config.max_body_bytes // BYTES_PER_MIB,
    )
    service = SendspinService(config)
    try:
        service.start()
        app = _create_app(service)
    except RuntimeError:
        logger.exception("Sendspin service startup failed")
        service.shutdown()
        return 1
    logger.info(
        "Sendspin service listening on http://%s:%s",
        config.api_host,
        config.api_port,
    )
    try:
        web.run_app(
            app,
            host=config.api_host,
            port=config.api_port,
            print=None,
            access_log=logger,
        )
    except OSError:
        logger.exception(
            "Sendspin service cannot listen on http://%s:%s",
            config.api_host,
            config.api_port,
        )
        return 1
    finally:
        service.shutdown()
    return 0
