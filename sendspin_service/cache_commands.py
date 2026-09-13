"""One explicit cache operation at a time, plus coalesced temporary-lap cleanup."""

from __future__ import annotations

import asyncio
import copy
import logging
import time
from typing import TYPE_CHECKING

from aiohttp import web

from .race_protocol import VERSION, ProtocolError

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from .speech import SpeechEngine

logger = logging.getLogger(__name__)


class CacheCommands:
    """Run manual preparation outside HTTP handlers with bounded retry history."""

    def __init__(
        self, speech: SpeechEngine, flush: Callable[[], Awaitable[None]]
    ) -> None:
        """Do no cache work until explicitly requested or a heat changes."""
        self._speech = speech
        self._flush = flush
        self._session = ""
        self._sequence = 0
        self._history: dict[str, tuple[dict, dict]] = {}
        self._task: asyncio.Task | None = None
        self._cleanup: asyncio.Task | None = None
        self._temporary: dict[str, dict] = {}
        self._clearing = False

    @property
    def clearing(self) -> bool:
        """Retain the admission barrier until the clear task has actually finished."""
        return self._clearing and self._task is not None and not self._task.done()

    def reset(self, session: str) -> None:
        """Cancel obsolete preparation and fence command IDs from the previous owner."""
        self.cancel()
        self._session = session
        self._sequence = 0
        self._history.clear()

    def cancel(self) -> None:
        """Stop producing additional phrases after a snapshot change."""
        if self._task is not None and not self._task.done():
            queued = False
            for _, result in self._history.values():
                if result["status"] in ("queued", "running"):
                    queued = result["status"] == "queued"
                    result["status"] = "cancelled"
            # Once started, deletion must retain its worker subscriber until done.
            # Cancelling that subscriber would leave disk work running unobserved.
            if not self.clearing or queued:
                self._task.cancel()

    def status(self, command_id: str) -> dict:
        """Never reinterpret an evicted or previous-session ID as a new operation."""
        if not command_id.startswith(f"{self._session}:command:"):
            raise web.HTTPConflict(reason="Stale command session")
        if command_id not in self._history:
            raise web.HTTPGone(reason="Command result unknown or expired")
        return dict(self._history[command_id][1])

    def submit(self, data: dict, snapshot: dict) -> dict:
        """Validate and admit one manual job without waiting for Piper or disk I/O."""
        command_id = data.get("command_id")
        prefix = f"{self._session}:command:"
        if not isinstance(command_id, str) or len(command_id) > 128:
            raise ProtocolError("Invalid command ID")
        suffix = command_id.removeprefix(prefix)
        if (
            not command_id.startswith(prefix)
            or not suffix.isascii()
            or not suffix.isdecimal()
            or str(int(suffix)) != suffix
            or int(suffix) < 1
        ):
            raise ProtocolError("Command ID requires session:command:sequence")
        if command_id in self._history:
            original, result = self._history[command_id]
            if original != data:
                raise web.HTTPConflict(
                    reason="Command ID reused with different content"
                )
            return dict(result)
        if int(suffix) <= self._sequence:
            raise web.HTTPGone(reason="Command result expired; not executed again")
        if data.get("version") != VERSION or data.get("operation") not in (
            "prepare",
            "clear_cache",
        ):
            raise ProtocolError("Unsupported cache command")
        revision = data.get("settings_revision")
        if (
            data.get("context") != snapshot["context"]
            or type(revision) is not int
            or revision != snapshot["context"]["revision"]
        ):
            raise web.HTTPConflict(reason="Cache command requires the current snapshot")
        if self._task is not None and not self._task.done():
            raise web.HTTPTooManyRequests(reason="A cache command is still running")
        self._clearing = data["operation"] == "clear_cache"
        self._sequence = int(suffix)
        result = {"command_id": command_id, "status": "queued"}
        self._history[command_id] = (copy.deepcopy(data), result)
        if len(self._history) > 32:
            del self._history[next(iter(self._history))]
        self._task = asyncio.create_task(
            self._run(data["operation"], copy.deepcopy(snapshot), result)
        )
        return dict(result)

    async def _run(self, operation: str, snapshot: dict, result: dict) -> None:
        result["status"] = "running"
        try:
            if operation == "prepare":
                names = [p["spoken_name"] or p["callsign"] for p in snapshot["pilots"]]
                async for progress in self._speech.prepare(
                    snapshot["voice"],
                    names,
                    is_current=lambda: result["status"] != "cancelled",
                ):
                    result.update(progress)
            else:
                await self._flush()
                cleared = await self._speech.worker.request(
                    {**snapshot["voice"], "operation": "clear"},
                    deadline=time.monotonic() + 120,
                    priority=-1,
                )
                result.update(cleared)
            if result["status"] != "cancelled":
                result["status"] = "completed"
        except asyncio.CancelledError:
            result["status"] = "cancelled"
            raise
        except Exception:
            logger.exception("Race Voice cache command failed")
            result.update(
                status="failed", error="Cache operation failed; see service log"
            )

    def clear_temporary(self, settings: dict) -> None:
        """Coalesce heat changes by model, retaining all prepared phrases."""
        self._temporary[settings["model"]] = dict(settings)
        if self._cleanup is None or self._cleanup.done():
            self._cleanup = asyncio.create_task(self._drain_temporary())

    async def _drain_temporary(self) -> None:
        while self._temporary:
            _, settings = self._temporary.popitem()
            try:
                await self._speech.worker.request(
                    {**settings, "operation": "clear", "subdir": "tmp"},
                    deadline=time.monotonic() + 120,
                    priority=20,
                )
            except Exception:
                logger.exception("Race Voice temporary cache cleanup failed")

    async def close(self) -> None:
        """Wait for both cache consumers before the owner reaps the worker."""
        tasks = [task for task in (self._task, self._cleanup) if task is not None]
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
