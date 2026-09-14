"""Optional browser player static routes for container deployments."""

from __future__ import annotations

import logging
from pathlib import Path

from aiohttp import web

logger = logging.getLogger(__name__)

PLAYER_INDEX = "index.html"
_PLAYER_INDEX_KEY: web.AppKey[Path] = web.AppKey("player_index", Path)


def add_player_routes(app: web.Application, player_dir: Path | None) -> None:
    """Serve the browser player when a built player directory is configured."""
    if player_dir is None:
        return

    index_path = player_dir / PLAYER_INDEX
    if not index_path.is_file():
        logger.warning("Ignoring missing player bundle: %s", player_dir)
        return

    app[_PLAYER_INDEX_KEY] = index_path
    app.router.add_get("/", _player_index)
    app.router.add_static("/", player_dir, show_index=False)
    logger.info("Sendspin service player available at /")


async def _player_index(request: web.Request) -> web.FileResponse:
    """Return the browser player entrypoint."""
    return web.FileResponse(request.app[_PLAYER_INDEX_KEY])
