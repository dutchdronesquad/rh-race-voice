"""Standalone HTTP service for Sendspin playback."""

from __future__ import annotations

import argparse
import contextlib
import hmac
import json
import logging
import os
from dataclasses import dataclass
from importlib.metadata import version
from pathlib import Path
from typing import TYPE_CHECKING, Any

from aiohttp import web

from .playback.audio_cache import AudioCache
from .playback.player import add_player_routes
from .playback.sendspin import SendSpinServer

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
# The systemd unit sets StateDirectory=sendspin-service and the Docker image
# and compose.yaml both mount persistent state at /var/lib/sendspin-service,
# so this default needs no additional packaging configuration to persist.
DEFAULT_RACE_CACHE_DIR = Path("/var/lib/sendspin-service/race-voice-cache")


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
    race_cache_dir: Path = DEFAULT_RACE_CACHE_DIR


class SendspinService:
    """Own Sendspin playback and expose simple service operations."""

    def __init__(self, config: ServiceConfig) -> None:
        """Initialize the service backend."""
        self._config = config
        if (
            config.api_host not in {"127.0.0.1", "::1", "localhost"}
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
            "bundled_audio": self._audio_cache.bundled_hashes,
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

    def shutdown(self) -> None:
        """Close the underlying Sendspin server."""
        self._sendspin.close()

    def add_race_routes(self, app: web.Application) -> None:
        """Load and register the race-event ingest routes and their dependencies."""
        from .race.race_ingest import RaceIngest, add_routes  # noqa: PLC0415
        from .race.race_planner import (  # noqa: PLC0415
            Destination,
            SendspinPlaybackSink,
        )
        from .synthesis.synthesis import SynthesisWorker  # noqa: PLC0415

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
            {
                "local": Destination(
                    SendspinPlaybackSink(self._sendspin, destination="local")
                )
            },
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
    service.add_race_routes(app)
    add_player_routes(app, service.player_dir)
    return app


async def _health(request: web.Request) -> web.Response:
    """Return service health metadata."""
    return web.json_response(_service(request).health())


@web.middleware
async def _api_token_middleware(
    request: web.Request,
    handler: web.RequestHandler,
) -> web.StreamResponse:
    if request.path.startswith("/v2/"):
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


def _service(request: web.Request) -> SendspinService:
    return request.app["service"]


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
        "--race-cache-dir",
        default=_env_path("SENDSPIN_RACE_CACHE_DIR") or DEFAULT_RACE_CACHE_DIR,
        type=Path,
        dest="race_cache_dir",
        help="Piper synthesis cache directory for the race-event ingest routes",
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
