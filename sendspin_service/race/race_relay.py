"""Forward synthesized audio and race context to a remote relay by content hash."""

from __future__ import annotations

import asyncio
import hashlib
import logging
import time
from typing import TYPE_CHECKING

import aiohttp

from . import telemetry

if TYPE_CHECKING:
    import threading

    from .race_planner import CalloutPlan
    from .race_protocol import Context

logger = logging.getLogger(__name__)

RELAY_VERSION = "race-relay/1"
_ASSETS_PATH = "/v2/relay/assets"
_EVENTS_PATH = "/v2/relay/events"
_STATE_PATH = "/v2/relay/state"
_CLOCK_PATH = "/v2/relay/clock"
_MAX_ATTEMPTS = 3
_RETRY_DELAY_S = 0.5
# Refresh a bit before the receiver's own ClockMapping.max_age (30s) would
# consider the mapping stale, so a slow attempt never races the expiry.
_CLOCK_REFRESH_S = 25.0


def _context_payload(context: Context) -> dict:
    return {
        "competition_id": context.competition_id,
        "revision": context.revision,
        "generation": context.generation,
        "heat_id": context.heat_id,
    }


class RaceRelaySink:
    """Forward a CalloutPlan's audio and context to a remote relay by content hash."""

    def __init__(
        self,
        base_url: str,
        *,
        token: str = "",
        destination: str = "relay",
        timeout_s: float = 5.0,
    ) -> None:
        """Keep construction free of network I/O; the session is created lazily."""
        self._base_url = base_url.rstrip("/")
        self._token = token
        self._destination = destination
        self._timeout = aiohttp.ClientTimeout(total=timeout_s)
        self._session: aiohttp.ClientSession | None = None
        self._clock_measured_at: float | None = None

    async def play(self, plan: CalloutPlan, cancelled: threading.Event) -> None:
        """Upload cache-miss audio, then forward the event by content reference."""
        event_id = plan.event.event_id
        if cancelled.is_set() or plan.deadline <= time.monotonic():
            telemetry.record(
                event_id,
                "output_dropped",
                reason="cancelled" if cancelled.is_set() else "expired",
                destination=self._destination,
            )
            return
        for attempt in range(_MAX_ATTEMPTS):
            try:
                await self._ensure_clock()
                hashes = [hashlib.sha256(data).hexdigest() for data in plan.audio]
                missing = await self._announce(hashes, plan.audio)
                for content_id, data in zip(hashes, plan.audio, strict=True):
                    if cancelled.is_set():
                        telemetry.record(
                            event_id,
                            "output_dropped",
                            reason="cancelled",
                            destination=self._destination,
                        )
                        return
                    if content_id in missing:
                        await self._upload(content_id, data)
                if cancelled.is_set():
                    telemetry.record(
                        event_id,
                        "output_dropped",
                        reason="cancelled",
                        destination=self._destination,
                    )
                    return
                await self._send_event(plan, hashes)
            except (aiohttp.ClientError, TimeoutError):
                out_of_attempts = attempt + 1 >= _MAX_ATTEMPTS
                expired = plan.deadline <= time.monotonic()
                if out_of_attempts or cancelled.is_set() or expired:
                    logger.warning(
                        "Sendspin relay: failed to deliver %s", event_id, exc_info=True
                    )
                    telemetry.record(
                        event_id,
                        "output_dropped",
                        reason="relay_error",
                        destination=self._destination,
                    )
                    return
                await asyncio.sleep(_RETRY_DELAY_S)
                continue
            else:
                telemetry.record(
                    event_id, "output_played", destination=self._destination
                )
                return

    async def push_context(self, context: Context) -> None:
        """Tell the relay which context is current, so it can reject stale events."""
        try:
            async with self._client().post(
                f"{self._base_url}{_STATE_PATH}",
                json={"version": RELAY_VERSION, "context": _context_payload(context)},
                headers=self._headers(),
            ) as response:
                response.raise_for_status()
        except (aiohttp.ClientError, TimeoutError):
            logger.warning("Sendspin relay: failed to push context", exc_info=True)

    async def stop(self) -> None:
        """No-op: an already-relayed event goes stale via push_context, not here."""
        return

    async def aclose(self) -> None:
        """Close the lazily-created client session. Not part of PlaybackSink."""
        if self._session is not None:
            await self._session.close()
            self._session = None

    def _client(self) -> aiohttp.ClientSession:
        if self._session is None:
            self._session = aiohttp.ClientSession(timeout=self._timeout)
        return self._session

    def _headers(self) -> dict[str, str]:
        if not self._token:
            return {}
        return {"Authorization": f"Bearer {self._token}"}

    async def _ensure_clock(self) -> None:
        measured_at = self._clock_measured_at
        fresh = (
            measured_at is not None
            and time.monotonic() - measured_at < _CLOCK_REFRESH_S
        )
        if fresh:
            return
        sent = time.monotonic()
        async with self._client().post(
            f"{self._base_url}{_CLOCK_PATH}",
            json={"version": RELAY_VERSION, "sent": sent},
            headers=self._headers(),
        ) as response:
            response.raise_for_status()
            probe = await response.json()
        received = time.monotonic()
        async with self._client().post(
            f"{self._base_url}{_CLOCK_PATH}",
            json={
                "version": RELAY_VERSION,
                "probe_id": probe["probe_id"],
                "received": received,
            },
            headers=self._headers(),
        ) as response:
            response.raise_for_status()
        self._clock_measured_at = time.monotonic()

    async def _announce(self, hashes: list[str], audio: tuple[bytes, ...]) -> set[str]:
        if not hashes:
            return set()
        refs = [
            {"sha256": content_id, "size": len(data)}
            for content_id, data in zip(hashes, audio, strict=True)
        ]
        async with self._client().post(
            f"{self._base_url}{_ASSETS_PATH}",
            json={"version": RELAY_VERSION, "refs": refs},
            headers=self._headers(),
        ) as response:
            response.raise_for_status()
            try:
                body = await response.json()
                missing = body["missing"]
            except (aiohttp.ContentTypeError, KeyError, ValueError):
                # Fail toward re-upload, never toward silently skipping audio.
                return set(hashes)
            if not isinstance(missing, list):
                return set(hashes)
            return set(missing)

    async def _upload(self, content_id: str, data: bytes) -> None:
        async with self._client().put(
            f"{self._base_url}{_ASSETS_PATH}/{content_id}",
            data=data,
            headers={**self._headers(), "Content-Type": "application/octet-stream"},
        ) as response:
            response.raise_for_status()

    async def _send_event(self, plan: CalloutPlan, hashes: list[str]) -> None:
        event = plan.event
        payload = {
            "version": RELAY_VERSION,
            "origin_event_id": event.event_id,
            "context": _context_payload(event.context),
            "kind": event.kind.value,
            "deadline": plan.deadline,
            "target": plan.target,
            "volume": plan.volume,
            "pilot_id": event.pilot_id,
            "text": event.text,
            "lap": event.lap,
            "pilot_name": event.pilot_name,
            "asset": event.asset,
            "winner": event.winner,
            "audio_refs": hashes,
        }
        async with self._client().post(
            f"{self._base_url}{_EVENTS_PATH}",
            json=payload,
            headers=self._headers(),
        ) as response:
            response.raise_for_status()
