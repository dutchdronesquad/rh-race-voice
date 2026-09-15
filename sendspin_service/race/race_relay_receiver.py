"""Accept pre-synthesized relay audio and play it through this host's own output."""

from __future__ import annotations

import hashlib
import json
import logging
import math
import re
import time
import uuid
from typing import TYPE_CHECKING

from aiohttp import web

from . import telemetry
from .race_planner import CalloutPlan, PlaybackPlanner
from .race_protocol import (
    MAX_EVENT_HORIZON,
    ClockMapping,
    Context,
    EventKind,
    ProtocolError,
    RaceEvent,
)
from .race_relay import RELAY_VERSION

if TYPE_CHECKING:
    from sendspin_service.playback.audio_cache import AudioCache

    from .race_planner import PlaybackSink

logger = logging.getLogger(__name__)

ASSETS_PATH = "/v2/relay/assets"
EVENTS_PATH = "/v2/relay/events"
STATE_PATH = "/v2/relay/state"
CLOCK_PATH = "/v2/relay/clock"
MAX_BODY_BYTES = 65_536
MAX_ASSET_BYTES = 8 * 1024 * 1024
MAX_SEEN_EVENTS = 32
_SHA256_RE = re.compile(r"[0-9a-f]{64}")


def _text(value: object, name: str, limit: int = 4096) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > limit:
        message = f"{name} must be nonempty text <= {limit} characters"
        raise ProtocolError(message)
    return value


def _number(value: object, name: str) -> float:
    if type(value) not in (int, float):
        message = f"{name} must be a number"
        raise ProtocolError(message)
    return float(value)


def _optional_int(value: object, name: str) -> int | None:
    if value is None:
        return None
    if type(value) is not int:
        message = f"{name} must be an integer"
        raise ProtocolError(message)
    return value


def _optional_text(value: object, name: str, limit: int = 4096) -> str | None:
    if value is None:
        return None
    return _text(value, name, limit)


def _check_version(data: dict) -> None:
    if data.get("version") != RELAY_VERSION:
        raise ProtocolError("Unsupported relay version")


class RaceRelayReceiver:
    """Accept pre-synthesized relay audio and play it through this host's own output."""

    def __init__(
        self, cache: AudioCache, sink: PlaybackSink, *, destination: str = "relay-in"
    ) -> None:
        """Own a planner independent of any RH-facing ingest on this same host."""
        self._cache = cache
        self._planner = PlaybackPlanner(
            sink, is_current=self._is_current, destination=destination
        )
        self._order = 0
        self._context: Context | None = None
        self._clock: ClockMapping | None = None
        self._probe: dict | None = None
        self._seen: dict[str, dict] = {}

    def _is_current(self, event: RaceEvent) -> bool:
        current = self._context
        return (
            current is not None
            and event.context.competition_id == current.competition_id
            and event.context.generation == current.generation
            and event.context.heat_id == current.heat_id
        )

    def assets(self, data: dict) -> dict:
        """Report which announced content hashes still need uploading."""
        _check_version(data)
        refs = data.get("refs")
        if not isinstance(refs, list) or not refs:
            raise ProtocolError("refs must be a non-empty list")
        hashes = [_content_hash(ref) for ref in refs]
        return {"missing": self._cache.missing(hashes)}

    def upload(self, sha256: str, data: bytes) -> None:
        """Verify the uploaded bytes match their claimed hash, then retain them."""
        if hashlib.sha256(data).hexdigest() != sha256:
            raise ProtocolError("Uploaded content does not match its hash")
        self._cache.store(sha256, data)

    def state(self, data: dict) -> dict:
        """Install the primary's current context, gating staleness from here on."""
        _check_version(data)
        self._context = Context.parse(data.get("context"))
        return {"outcome": "accepted"}

    def clock(self, data: dict) -> dict:
        """Keep one short-lived clock probe per relay, mirroring RaceIngest.clock."""
        _check_version(data)
        now = time.monotonic()
        if "probe_id" not in data:
            sent = data.get("sent")
            if type(sent) not in (int, float) or not math.isfinite(sent):
                raise ProtocolError("Clock probe requires a finite sent timestamp")
            self._probe = {
                "probe_id": uuid.uuid4().hex,
                "sent": sent,
                "received_remote": now,
                "sent_remote": time.monotonic(),
            }
            return dict(self._probe)
        probe = self._probe
        if (
            probe is None
            or data["probe_id"] != probe["probe_id"]
            or now - probe["sent_remote"] > 5
        ):
            raise web.HTTPConflict(reason="Clock probe expired or replaced")
        self._probe = None
        self._clock = ClockMapping.from_exchange(
            probe["sent"],
            probe["received_remote"],
            probe["sent_remote"],
            data.get("received"),
        )
        return {"offset": self._clock.offset, "uncertainty": self._clock.uncertainty}

    def event(self, data: dict) -> dict:
        """Reconstruct a CalloutPlan from a relayed event and queue it for playback."""
        origin_event_id = _text(data.get("origin_event_id"), "origin_event_id", 256)
        seen = self._seen.get(origin_event_id)
        if seen is not None:
            return seen
        self._order += 1
        plan = _parse_plan(data, self._cache, self._order, self._clock)
        telemetry.record(
            plan.event.event_id, "received", kind=plan.event.kind.value, origin="relay"
        )
        result = {"outcome": "accepted" if self._planner.submit(plan) else "dropped"}
        self._seen[origin_event_id] = result
        if len(self._seen) > MAX_SEEN_EVENTS:
            del self._seen[next(iter(self._seen))]
        return result

    async def close(self) -> None:
        """Stop accepting and finish this destination's own planner."""
        await self._planner.close()


def _content_hash(ref: object) -> str:
    if not isinstance(ref, dict) or not _SHA256_RE.fullmatch(str(ref.get("sha256"))):
        raise ProtocolError("Each ref must carry a sha256 content hash")
    return ref["sha256"]


def _parse_plan(
    data: dict, cache: AudioCache, order: int, clock: ClockMapping | None
) -> CalloutPlan:
    """Reconstruct a CalloutPlan from a race-relay/1 event body."""
    _check_version(data)
    if clock is None:
        raise ProtocolError("No relay clock mapping established yet")
    event_id = _text(data.get("origin_event_id"), "origin_event_id", 256)
    context = Context.parse(data.get("context"))
    try:
        kind = EventKind(data.get("kind"))
    except (ValueError, TypeError) as err:
        raise ProtocolError("Unsupported event kind") from err
    now = time.monotonic()
    deadline = clock.bounds(_number(data.get("deadline"), "deadline"), now)[1]
    if not 0 < deadline - now <= MAX_EVENT_HORIZON:
        raise ProtocolError("Invalid event lifetime")
    raw_target = data.get("target")
    target = clock.bounds(_number(raw_target, "target"), now)[1] if raw_target else None
    volume = _number(data.get("volume", 1.0), "volume")
    audio_refs = data.get("audio_refs")
    if not isinstance(audio_refs, list) or not audio_refs:
        raise ProtocolError("audio_refs must be a non-empty list")
    audio = []
    for ref in audio_refs:
        if not _SHA256_RE.fullmatch(str(ref)):
            raise ProtocolError("Each audio ref must be a sha256 content hash")
        segment = cache.get(ref)
        if segment is None:
            message = f"Unresolved audio reference: {ref}"
            raise ProtocolError(message)
        audio.append(segment)
    event = RaceEvent(
        session_id=event_id,
        event_id=event_id,
        sequence=order,
        context=context,
        kind=kind,
        occurred_at=now,
        expires_at=deadline,
        play_at=target,
        pilot_id=_optional_int(data.get("pilot_id"), "pilot_id"),
        text=_optional_text(data.get("text"), "text"),
        lap=_optional_int(data.get("lap"), "lap"),
        pilot_name=_optional_text(data.get("pilot_name"), "pilot_name", 256),
        asset=_optional_text(data.get("asset"), "asset", 64),
        winner=bool(data.get("winner", False)),
    )
    return CalloutPlan(event, deadline, target, volume, tuple(audio), order=order)


async def _read_json_body(request: web.Request, limit: int) -> dict:
    body = await _read_raw_body(request, limit)
    data = json.loads(body)
    if not isinstance(data, dict):
        raise ProtocolError("JSON body must be an object")
    return data


async def _read_raw_body(request: web.Request, limit: int) -> bytes:
    body = bytearray()
    async for chunk in request.content.iter_chunked(8192):
        body.extend(chunk)
        if len(body) > limit:
            raise web.HTTPRequestEntityTooLarge(max_size=limit, actual_size=len(body))
    return bytes(body)


def _validate_sha256(value: str) -> str:
    if not _SHA256_RE.fullmatch(value):
        raise ProtocolError("Invalid content hash")
    return value


def add_routes(app: web.Application, receiver: RaceRelayReceiver) -> None:
    """Register the race-relay/1 receiver routes."""
    handlers = {
        ASSETS_PATH: receiver.assets,
        STATE_PATH: receiver.state,
        CLOCK_PATH: receiver.clock,
    }

    async def handle(request: web.Request) -> web.Response:
        try:
            if "sha256" in request.match_info:
                sha256 = _validate_sha256(request.match_info["sha256"])
                body = await _read_raw_body(request, MAX_ASSET_BYTES)
                receiver.upload(sha256, body)
                return web.json_response({})
            data = await _read_json_body(request, MAX_BODY_BYTES)
            json_handler = handlers.get(request.path)
            if json_handler is not None:
                return web.json_response(json_handler(data))
            result = receiver.event(data)
            status = 202 if result["outcome"] == "accepted" else 200
            return web.json_response(result, status=status)
        except web.HTTPException as err:
            return web.json_response({"error": err.reason}, status=err.status)
        except (ValueError, TypeError, OverflowError) as err:
            return web.json_response({"error": str(err)}, status=400)
        except Exception:
            logger.exception("Sendspin relay receive failed: %s", request.path)
            return web.json_response({"error": "Race relay receive failed"}, status=500)

    app.router.add_post(ASSETS_PATH, handle)
    app.router.add_put(f"{ASSETS_PATH}/{{sha256}}", handle)
    app.router.add_post(STATE_PATH, handle)
    app.router.add_post(CLOCK_PATH, handle)
    app.router.add_post(EVENTS_PATH, handle)
