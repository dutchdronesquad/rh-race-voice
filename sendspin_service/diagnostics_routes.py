"""Authenticated administrative capture controls, separate from listener controls."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

from aiohttp import web

if TYPE_CHECKING:
    from .diagnostics import DiagnosticCapture


def add_routes(app: web.Application, capture: DiagnosticCapture) -> None:
    """Keep file and archive operations off both service and Sendspin event loops."""

    async def handle(request: web.Request) -> web.Response:
        try:
            if request.match_info["action"] == "export":
                data = await asyncio.to_thread(capture.export)
                return web.Response(
                    body=data,
                    content_type="application/zip",
                    headers={
                        "Content-Disposition": (
                            'attachment; filename="race-diagnostics.zip"'
                        )
                    },
                )
            operation = {
                "start": capture.start,
                "stop": capture.stop,
                "status": capture.status,
            }[request.match_info["action"]]
            return web.json_response(await asyncio.to_thread(operation))
        except FileNotFoundError as exc:
            raise web.HTTPNotFound(reason="No diagnostic recording yet") from exc
        except (OSError, RuntimeError, ValueError) as exc:
            raise web.HTTPServiceUnavailable(
                reason="Diagnostic storage is unavailable"
            ) from exc

    app.router.add_get("/v2/diagnostics/{action:status|export}", handle)
    app.router.add_post("/v2/diagnostics/{action:start|stop}", handle)

    async def cleanup(_app: web.Application) -> None:
        await asyncio.to_thread(capture.stop)

    app.on_cleanup.append(cleanup)
