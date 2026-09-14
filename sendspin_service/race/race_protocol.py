"""Pure reference types for the proposed race-events/1 contract.

No endpoint advertises this capability until the complete consumer is wired.
The admission gate is owned by one event loop, not shared between threads.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from enum import StrEnum

VERSION = "race-events/1"
MAX_TEXT = 4096
MAX_EVENT_HORIZON = 3600.0


class ProtocolError(ValueError):
    """Reject malformed or unsafe race protocol input."""


def _integer(value: object, name: str, minimum: int = 0) -> int:
    if type(value) is not int or value < minimum:
        message = f"{name} must be an integer >= {minimum}"
        raise ProtocolError(message)
    return value


def _text(value: object, name: str, limit: int = MAX_TEXT) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > limit:
        message = f"{name} must be nonempty text <= {limit} characters"
        raise ProtocolError(message)
    return value


def _number(value: object, name: str) -> float:
    if type(value) not in (int, float):
        raise ProtocolError("Number required")
    try:
        value = float(value)
    except OverflowError as err:
        raise ProtocolError("Number is out of range") from err
    if not math.isfinite(value):
        message = f"{name} must be finite"
        raise ProtocolError(message)
    return float(value)


def _object(value: object, name: str) -> dict:
    if not isinstance(value, dict):
        message = f"{name} must be an object"
        raise ProtocolError(message)
    return value


class EventKind(StrEnum):
    """Semantic audio classes; tones never require synthesis."""

    LAP = "lap"
    VOICE = "voice"
    COUNTDOWN = "countdown"
    TONE = "tone"


@dataclass(frozen=True)
class Context:
    """Immutable source state identity captured with every event."""

    competition_id: str
    revision: int
    generation: int
    heat_id: int | None

    @classmethod
    def parse(cls, value: object) -> Context:
        """Validate identity, state revision and audio cancellation generation."""
        data = _object(value, "context")
        heat_id = data.get("heat_id")
        if heat_id is not None:
            heat_id = _integer(heat_id, "heat_id", 1)
        return cls(
            _text(data.get("competition_id"), "competition_id", 128),
            _integer(data.get("revision"), "revision", 1),
            _integer(data.get("generation"), "generation"),
            heat_id,
        )


@dataclass(frozen=True)
class RaceEvent:
    """An immutable audio request in the publisher's monotonic clock domain."""

    session_id: str
    event_id: str
    sequence: int
    context: Context
    kind: EventKind
    occurred_at: float
    expires_at: float
    play_at: float | None
    pilot_id: int | None
    text: str | None
    lap: int | None
    pilot_name: str | None
    asset: str | None
    winner: bool

    @classmethod
    def parse(cls, value: object) -> RaceEvent:
        """Reject unknown kinds, nonfinite times and incomplete callouts."""
        data = _object(value, "event")
        if data.get("version") != VERSION:
            raise ProtocolError("Unsupported event version")
        try:
            kind = EventKind(data.get("kind"))
        except (ValueError, TypeError) as err:
            raise ProtocolError("Unsupported event kind") from err
        occurred_at = _number(data.get("occurred_at"), "occurred_at")
        expires_at = _number(data.get("expires_at"), "expires_at")
        if not 0 < expires_at - occurred_at <= MAX_EVENT_HORIZON:
            raise ProtocolError("Invalid event lifetime")
        play_at = data.get("play_at")
        if play_at is not None:
            play_at = _number(play_at, "play_at")
            if play_at >= expires_at:
                raise ProtocolError("Scheduled target must precede expiry")
        payload = _object(data.get("payload"), "payload")
        pilot_id, text, lap, pilot_name, asset = _payload(kind, payload)
        winner = payload.get("winner_flag", False)
        if type(winner) is not bool:
            raise ProtocolError("winner_flag must be boolean")
        session_id = _text(data.get("session_id"), "session_id", 128)
        sequence = _integer(data.get("sequence"), "sequence", 1)
        event_id = _text(data.get("event_id"), "event_id", 256)
        if event_id != f"{session_id}:{sequence}":
            raise ProtocolError("Event ID must be derived from session and sequence")
        return cls(
            session_id,
            event_id,
            sequence,
            Context.parse(data.get("context")),
            kind,
            occurred_at,
            expires_at,
            play_at,
            pilot_id,
            text,
            lap,
            pilot_name,
            asset,
            winner,
        )


def _payload(kind: EventKind, value: object) -> tuple:
    payload = _object(value, "payload")
    text = pilot_name = asset = None
    pilot_id = lap = None
    if kind == EventKind.TONE:
        asset = payload.get("asset")
        if asset not in ("stage", "buzzer", "audio_check"):
            raise ProtocolError("Unknown bundled tone asset")
    else:
        text = _text(payload.get("text"), "text")
    if kind == EventKind.LAP:
        pilot_id = payload.get("pilot_id")
        if pilot_id is not None:
            pilot_id = _integer(pilot_id, "pilot_id", 1)
        lap = _integer(payload.get("lap"), "lap", 1)
        pilot_name = payload.get("pilot_name", "")
        if not isinstance(pilot_name, str) or len(pilot_name) > 256:
            raise ProtocolError("pilot_name must be text <= 256 characters")
    return pilot_id, text, lap, pilot_name, asset


@dataclass(frozen=True)
class ClockMapping:
    """Bound an offset using a four-timestamp exchange and oscillator drift."""

    offset: float
    uncertainty: float
    measured_at: float
    max_age: float = 30.0
    drift_per_second: float = 0.0001

    @classmethod
    def from_exchange(
        cls,
        sent: float,
        received_remote: float,
        sent_remote: float,
        received: float,
    ) -> ClockMapping:
        """Map publisher timestamps to destination time without assuming symmetry."""
        sent, received_remote, sent_remote, received = (
            _number(value, "clock timestamp")
            for value in (sent, received_remote, sent_remote, received)
        )
        if received < sent or sent_remote < received_remote:
            raise ProtocolError("Clock exchange ran backwards")
        lower = sent_remote - received
        upper = received_remote - sent
        if lower > upper:
            raise ProtocolError("Impossible clock exchange")
        return cls((lower + upper) / 2, (upper - lower) / 2, sent_remote)

    def bounds(self, publisher_time: float, now: float) -> tuple[float, float]:
        """Return destination bounds or require a fresh exchange after a gap."""
        publisher_time = _number(publisher_time, "publisher timestamp")
        now = _number(now, "destination timestamp")
        age = now - self.measured_at
        if age < 0 or age > self.max_age:
            raise ProtocolError("Clock mapping needs refreshing")
        error = self.uncertainty + age * self.drift_per_second
        centre = publisher_time + self.offset
        return centre - error, centre + error

    def expiry(self, publisher_time: float, now: float) -> float:
        """Expire conservatively at the earliest possible destination deadline."""
        return self.bounds(publisher_time, now)[0]


class Admission(StrEnum):
    """Terminal admission outcomes; accepted does not mean audible."""

    ACCEPTED = "accepted"
    DUPLICATE = "duplicate"
    STALE_SESSION = "stale_session"
    STALE_CONTEXT = "stale_context"
    NEED_SNAPSHOT = "need_snapshot"
    EXPIRED = "expired"


class ContextGate:
    """Fence sessions, state and retries before audio reaches a bounded queue."""

    def __init__(self, session_id: str) -> None:
        """Create a fresh gate only after an authorized session takeover."""
        self.session_id = _text(session_id, "session_id", 128)
        self.context: Context | None = None
        self.sequence = 0

    def snapshot(self, session_id: str, context: Context) -> Admission:
        """Install a full snapshot without allowing rollback or generation reuse."""
        if session_id != self.session_id:
            return Admission.STALE_SESSION
        old = self.context
        if old is not None:
            if context.revision <= old.revision:
                return (
                    Admission.DUPLICATE if context == old else Admission.STALE_CONTEXT
                )
            identity_changed = (
                context.competition_id != old.competition_id
                or context.heat_id != old.heat_id
            )
            if context.generation < old.generation or (
                identity_changed and context.generation == old.generation
            ):
                return Admission.STALE_CONTEXT
        self.context = context
        return Admission.ACCEPTED

    def admit(self, event: RaceEvent, deadline: float, now: float) -> Admission:
        """Consume sequence order even for expired/stale events to prevent replay."""
        deadline = _number(deadline, "destination deadline")
        now = _number(now, "destination now")
        if event.session_id != self.session_id:
            return Admission.STALE_SESSION
        if event.sequence <= self.sequence:
            return Admission.DUPLICATE
        if self.context is None or event.context.revision > self.context.revision:
            return Admission.NEED_SNAPSHOT
        self.sequence = event.sequence
        if event.context != self.context:
            return Admission.STALE_CONTEXT
        if deadline <= now:
            return Admission.EXPIRED
        return Admission.ACCEPTED

    def is_current(self, event: RaceEvent) -> bool:
        """Recheck worker results against the session and cancellation generation."""
        current = self.context
        return (
            current is not None
            and event.session_id == self.session_id
            and event.context.competition_id == current.competition_id
            and event.context.generation == current.generation
            and event.context.heat_id == current.heat_id
        )
