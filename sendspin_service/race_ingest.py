"""Opt-in HTTP receiver for one race source and one local Sendspin output."""

from __future__ import annotations

import json
import logging
import math
import time
import uuid
from typing import TYPE_CHECKING

from aiohttp import web

from custom_plugins.race_voice.const import VOICE_MODELS

from .race_planner import CalloutPlan, PlaybackPlanner, PreparationPlanner
from .race_protocol import (
    VERSION,
    Admission,
    ClockMapping,
    Context,
    ContextGate,
    EventKind,
    ProtocolError,
    RaceEvent,
)
from .speech import SpeechEngine

if TYPE_CHECKING:
    from .race_planner import PlaybackSink
    from .synthesis import SynthesisWorker

logger = logging.getLogger(__name__)
MAX_BODY_BYTES = 65_536


def _text(value: object, limit: int, *, empty: bool = False) -> bool:
    return (
        isinstance(value, str)
        and len(value) <= limit
        and (empty or bool(value.strip()))
    )


def _snapshot(data: dict) -> tuple[Context, str]:
    """Validate captured settings/roster before changing any live state."""
    if data.get("version") != VERSION:
        raise ProtocolError("Unsupported snapshot version")
    context = Context.parse(data.get("context"))
    if not _text(data.get("heat_name"), 256, empty=True):
        raise ProtocolError("Invalid heat name")
    pilots = data.get("pilots")
    if not isinstance(pilots, list) or len(pilots) > 256:
        raise ProtocolError("Invalid pilot roster")
    ids = [_pilot_id(pilot) for pilot in pilots]
    if len(set(ids)) != len(ids):
        raise ProtocolError("Duplicate pilot ID")
    voice = data.get("voice")
    if not isinstance(voice, dict) or voice.get("model") not in VOICE_MODELS:
        raise ProtocolError("Unknown voice model")
    for key in ("speed", "noise", "noise_w"):
        value = voice.get(key)
        minimum = 0.1 if key == "speed" else 0
        if type(value) not in (int, float) or not minimum <= value <= 10:
            raise ProtocolError("Invalid voice tuning")
    flags = voice.get("callout_flags")
    if type(voice.get("enabled")) is not bool or not isinstance(flags, dict):
        raise ProtocolError("Invalid callout settings")
    if any(
        key not in {kind.value for kind in EventKind} or type(value) is not bool
        for key, value in flags.items()
    ):
        raise ProtocolError("Unknown callout flag; use lap, voice, countdown or tone")
    return context, json.dumps(data, sort_keys=True, allow_nan=False)


def _pilot_id(pilot: object) -> int:
    """Validate one roster entry without interpreting names as identity."""
    if not isinstance(pilot, dict):
        raise ProtocolError("Invalid pilot")
    pilot_id = pilot.get("pilot_id")
    if type(pilot_id) is not int or pilot_id < 1:
        raise ProtocolError("Invalid or duplicate pilot ID")
    if not _text(pilot.get("callsign"), 128) or not _text(
        pilot.get("spoken_name"), 256, empty=True
    ):
        raise ProtocolError("Invalid pilot name")
    if "name" in pilot and not _text(pilot["name"], 256, empty=True):
        raise ProtocolError("Invalid display name")
    node = pilot.get("node")
    if node is not None and (type(node) is not int or node < 0):
        raise ProtocolError("Invalid node")
    if pilot.get("colour") is not None and not _text(pilot["colour"], 32, empty=True):
        raise ProtocolError("Invalid colour")
    return pilot_id


class RaceIngest:
    """Own admission and cancellation on the HTTP loop; never wait for inference."""

    def __init__(
        self, worker: SynthesisWorker, sink: PlaybackSink, assets: dict[str, bytes]
    ) -> None:
        """Wire the existing worker and planners into one local output."""
        self._worker = worker
        self._boot_id = uuid.uuid4().hex
        self._owner_revision = 0
        self._epoch = self._nonce = ""
        self._gate = ContextGate(uuid.uuid4().hex)
        self._state: dict | None = None
        self._state_json = ""
        self._clock: ClockMapping | None = None
        self._probe: dict | None = None
        self._changing = False
        self._blocked = True
        self._output = PlaybackPlanner(sink, is_current=self._current)
        self._preparation = PreparationPlanner(
            SpeechEngine(worker),
            assets=assets,
            is_current=self._current,
            ready=self._ready,
        )

    def _current(self, event: RaceEvent) -> bool:
        return not self._blocked and self._gate.is_current(event)

    def _ready(self, plan: CalloutPlan) -> None:
        if not self._output.submit(plan):
            logger.info("Race Voice dropped prepared audio: %s", plan.event.event_id)

    def owner(self) -> dict:
        """Expose ownership and retry recovery under the configured API token policy."""
        return {
            "boot_id": self._boot_id,
            "owner_revision": self._owner_revision,
            "epoch": self._epoch,
            "nonce": self._nonce,
            "session_id": self._gate.session_id if self._epoch else None,
        }

    async def session(self, data: dict) -> dict:
        """Fence a previous publisher before returning a fresh session."""
        epoch, nonce = data.get("epoch"), data.get("nonce")
        if not _text(epoch, 128) or not _text(nonce, 128):
            raise ProtocolError("Session requires epoch and nonce")
        if self._changing:
            raise web.HTTPConflict(reason="State change in progress")
        if data.get("boot_id") != self._boot_id:
            raise web.HTTPConflict(reason="Service restarted; retrieve ownership")
        retry = epoch == self._epoch and nonce == self._nonce
        if not retry:
            revision = data.get("owner_revision")
            if type(revision) is not int or revision != self._owner_revision:
                raise web.HTTPConflict(reason="Ownership changed; retrieve ownership")
            if (
                self._epoch
                and epoch != self._epoch
                and data.get("takeover") is not True
            ):
                raise web.HTTPConflict(
                    reason="Another publisher owns this source; takeover required"
                )
            self._epoch, self._nonce = epoch, nonce
            self._owner_revision += 1
            self._gate = ContextGate(uuid.uuid4().hex)
            self._state = None
            self._state_json = ""
            self._clock = self._probe = None
        if not retry or self._blocked:
            await self._clear()
        return self.owner()

    def _require_session(self, data: dict) -> None:
        if not self._epoch or data.get("session_id") != self._gate.session_id:
            raise web.HTTPConflict(reason="Stale or missing session")
        if self._changing:
            raise web.HTTPConflict(reason="State change in progress")

    async def state(self, data: dict) -> dict:
        """Apply a complete snapshot and acknowledge only after output clear."""
        self._require_session(data)
        context, encoded = _snapshot(data)
        old = self._gate.context
        if (
            old is not None
            and context.revision == old.revision
            and encoded != self._state_json
        ):
            raise web.HTTPConflict(
                reason="Snapshot revision reused with different content"
            )
        if (
            old is not None
            and self._state is not None
            and data["voice"] != self._state["voice"]
            and context.generation <= old.generation
        ):
            raise web.HTTPConflict(reason="Voice change requires a new generation")
        outcome = self._gate.snapshot(data["session_id"], context)
        if outcome not in (Admission.ACCEPTED, Admission.DUPLICATE):
            raise web.HTTPConflict(reason=outcome.value)
        self._state = json.loads(encoded)
        self._state_json = encoded
        if old is None or old.generation != context.generation or self._blocked:
            await self._clear()
        return {"outcome": outcome.value}

    async def _clear(self) -> None:
        self._changing = self._blocked = True
        self._preparation.invalidate()
        try:
            await self._output.flush()
            self._blocked = False
        finally:
            self._changing = False

    def clock(self, data: dict) -> dict:
        """Keep one short-lived, server-owned clock probe per publisher."""
        self._require_session(data)
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
        """Admit bounded work immediately; final expiry is checked again at playback."""
        self._require_session(data)
        event = RaceEvent.parse(data)
        if self._blocked:
            raise web.HTTPConflict(reason="Output clear failed; retry state update")
        if self._clock is None:
            raise web.HTTPConflict(reason="Clock exchange required")
        now = time.monotonic()
        try:
            deadline = self._clock.expiry(event.expires_at, now)
            target = None
            if event.play_at is not None:
                lower, upper = self._clock.bounds(event.play_at, now)
                if event.kind == EventKind.TONE and upper - lower > 0.2:
                    raise web.HTTPConflict(
                        reason="Scheduled tone clock uncertainty exceeds 100 ms"
                    )
                target = (lower + upper) / 2
        except ProtocolError as err:
            raise web.HTTPConflict(reason=str(err)) from err
        outcome = self._gate.admit(event, deadline, now)
        if outcome != Admission.ACCEPTED:
            return {"outcome": outcome.value}
        voice = self._state["voice"]
        if not voice["enabled"] or not voice["callout_flags"].get(
            event.kind.value, True
        ):
            return {"outcome": "disabled"}
        if not self._preparation.submit(CalloutPlan(event, deadline, target), voice):
            raise web.HTTPTooManyRequests(
                reason="Audio preparation is full", headers={"Retry-After": "1"}
            )
        return {"outcome": "accepted"}

    async def close(self) -> None:
        """Cancel preparation and playback before reaping the synthesis child."""
        self._blocked = True
        try:
            await self._preparation.close()
            await self._output.close()
        finally:
            await self._worker.close()


def add_routes(app: web.Application, ingest: RaceIngest) -> None:
    """Register preview routes; the app enforces auth when an API token is set."""

    async def handle(request: web.Request) -> web.Response:
        try:
            if request.method == "GET":
                return web.json_response(ingest.owner())
            data = await _read_body(request)
            match request.path:
                case "/v2/session":
                    result = await ingest.session(data)
                case "/v2/state":
                    result = await ingest.state(data)
                case "/v2/clock":
                    result = ingest.clock(data)
                case _:
                    result = ingest.event(data)
            status = (
                202
                if result.get("outcome") == "accepted" and request.path == "/v2/events"
                else 200
            )
            if result.get("outcome") in {
                "stale_session",
                "stale_context",
                "need_snapshot",
            }:
                status = 409
            return web.json_response(result, status=status)
        except web.HTTPException as err:
            return web.json_response(
                {"error": err.reason},
                status=err.status,
                headers={
                    key: value
                    for key, value in err.headers.items()
                    if key.lower() == "retry-after"
                },
            )
        except (ValueError, TypeError, OverflowError) as err:
            return web.json_response({"error": str(err)}, status=400)
        except Exception:
            logger.exception("Race Voice ingest failed: %s", request.path)
            return web.json_response(
                {"error": "Race output operation failed"}, status=500
            )

    app.router.add_get("/v2/session", handle)
    app.router.add_post("/v2/session", handle)
    app.router.add_put("/v2/state", handle)
    app.router.add_post("/v2/clock", handle)
    app.router.add_post("/v2/events", handle)


async def _read_body(request: web.Request) -> dict:
    """Bound chunked input independently of the legacy audio-upload limit."""
    body = bytearray()
    async for chunk in request.content.iter_chunked(8192):
        body.extend(chunk)
        if len(body) > MAX_BODY_BYTES:
            raise web.HTTPRequestEntityTooLarge(
                max_size=MAX_BODY_BYTES, actual_size=len(body)
            )
    data = json.loads(body)
    if not isinstance(data, dict):
        raise ProtocolError("JSON body must be an object")
    return data
