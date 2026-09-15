"""Bounded event delivery on RotorHazard's gevent hub, without RH API access."""

from __future__ import annotations

import copy
import http.client
import json
import logging
import time
import uuid
from urllib.parse import urlsplit

import gevent
from gevent.event import Event

logger = logging.getLogger(__name__)
_LIMIT = 65_536


def next_counter(previous: int) -> int:
    """Advance past *previous*, anchored on wall-clock ms (see event_adapter)."""
    return max(previous + 1, int(time.time() * 1000))


class JsonChannel:
    """Reuse one HTTP connection, owned by exactly one sender greenlet."""

    def __init__(self, token: str) -> None:
        """Defer connections until background delivery."""
        self._token = token
        self._url = ""
        self._connection = None

    def request(
        self, url: str, method: str, path: str, data: dict | None = None
    ) -> tuple[int, dict]:
        """Bound total request time and both message sizes; do not follow redirects."""
        body = json.dumps(data, allow_nan=False).encode() if data is not None else None
        if body is not None and len(body) > _LIMIT:
            raise ValueError("Race event message exceeds 64 KiB")
        endpoint = urlsplit(url)
        if (
            endpoint.scheme not in {"http", "https"}
            or not endpoint.hostname
            or endpoint.username is not None
            or endpoint.password is not None
            or endpoint.query
            or endpoint.fragment
        ):
            raise ValueError("Invalid event service URL")
        try:
            with gevent.Timeout(3, TimeoutError("Event service request timed out")):
                if self._connection is None or self._url != url:
                    self.close()
                    connection = (
                        http.client.HTTPSConnection
                        if endpoint.scheme == "https"
                        else http.client.HTTPConnection
                    )
                    self._connection = connection(
                        endpoint.hostname, endpoint.port, timeout=2
                    )
                    self._url = url
                headers = {
                    "Content-Type": "application/json",
                    "User-Agent": "RaceVoice/1.0",
                }
                if self._token:
                    headers["Authorization"] = f"Bearer {self._token}"
                self._connection.request(
                    method, endpoint.path.rstrip("/") + path, body=body, headers=headers
                )
                response = self._connection.getresponse()
                raw = response.read(_LIMIT + 1)
                if len(raw) > _LIMIT:
                    raise ValueError("Event service response exceeds 64 KiB")  # noqa: TRY301
                result = json.loads(raw)
                if not isinstance(result, dict):
                    raise TypeError("Event service returned invalid JSON")  # noqa: TRY301
                return response.status, result
        except BaseException:
            self.close()
            raise

    def close(self) -> None:
        """Discard broken or superseded connections."""
        if self._connection is not None:
            self._connection.close()
            self._connection = None


class EventPublisher:
    """Keep one latest snapshot and small disposable queues, independent of TTS."""

    def __init__(self, *, epoch: str, token: str = "") -> None:
        """Keep construction free of network I/O.

        *epoch* identifies this publisher across process restarts (the
        caller persists it), so a restart reclaims its own session without
        a conflict, while a genuinely different publisher still fences.
        """
        self._control = JsonChannel(token)
        self._audio = JsonChannel(token)
        self._commands = JsonChannel(token)
        self._command_task = None
        self._command_sequence = 0
        self.command_status = "No cache command requested"
        self._epoch = epoch
        self._url = ""
        self._state: dict | None = None
        self._pending: list[dict] = []
        self._session: str | None = None
        self._sequence = 0
        self._last_session = None
        self._serial = 0
        self._ack_revision = 0
        self._clock_due = 0.0
        self._handshake_complete = False
        self._wake = Event()
        self._audio_wake = Event()
        self._tasks = []
        self._closed = False
        self.status = "Not connected"

    def set_epoch(self, epoch: str) -> None:
        """Replace the identity set at construction, before the first connect."""
        self._epoch = epoch

    def start(self) -> None:
        """Start cooperative senders after RH startup, once state has been captured."""
        if not self._tasks and not self._closed:
            self._tasks = [
                gevent.spawn(self._run_control),
                gevent.spawn(self._run_audio),
            ]

    def update(self, snapshot: dict, url: str) -> None:
        """Replace pending control state; callbacks perform no network operations."""
        try:
            snapshot = copy.deepcopy(snapshot)
            encoded = json.dumps(snapshot, allow_nan=False).encode()
        except (TypeError, ValueError):
            self._state = None
            self._disconnect("Invalid race state; waiting for a new snapshot")
            raise
        if len(encoded) > _LIMIT - 256:
            self._state = None
            self._disconnect("Race state exceeds 64 KiB")
            raise ValueError("Race state exceeds 64 KiB")
        self._state = snapshot
        self._pending.clear()
        self._ack_revision = 0
        if url != self._url:
            self._url = url
            self._disconnect("Service URL changed")
        elif self._session is not None:
            self._set_status("Updating race state")
        self._wake.set()

    def submit(
        self,
        kind: str,
        payload: dict,
        *,
        expires_at: float,
        play_at: float | None = None,
    ) -> bool:
        """Capture fresh audio without waiting; drop work while disconnected."""
        if not self.can_send_audio() or expires_at <= time.monotonic():
            return False
        event = {
            "version": "race-events/1",
            "context": copy.deepcopy(self._state["context"]),
            "kind": kind,
            "occurred_at": time.monotonic(),
            "expires_at": expires_at,
            "payload": copy.deepcopy(payload),
        }
        if play_at is not None:
            event["play_at"] = play_at
        if len(json.dumps(event, allow_nan=False).encode()) > _LIMIT - 256:
            return False
        self._pending = [
            item for item in self._pending if item["expires_at"] > time.monotonic()
        ]
        if _priority(event) == 0:
            self._pending = [item for item in self._pending if item["kind"] != "lap"]
        same = [
            item for item in self._pending if (item["kind"] == "lap") == (kind == "lap")
        ]
        if kind == "lap":
            pilot = payload.get("pilot_id")
            replacement = next(
                (
                    item
                    for item in same
                    if pilot is not None and item["payload"].get("pilot_id") == pilot
                ),
                None,
            )
            if replacement is None and len(same) >= 4:
                replacement = same[0]
            if replacement is not None:
                self._pending.remove(replacement)
        elif len(same) >= 32:
            lower = next(
                (item for item in same if _priority(item) > _priority(event)), None
            )
            if lower is None:
                return False
            self._pending.remove(lower)
        self._pending.append(event)
        self._audio_wake.set()
        return True

    def _ready(self) -> bool:
        return bool(
            not self._closed
            and self._session
            and self._state
            and self._ack_revision == self._state["context"]["revision"]
        )

    def can_send_audio(self) -> bool:
        """Check whether this session has done its first full handshake.

        Unlike _ready(), this does not wait for every subsequent state PUT
        to be acknowledged -- the server's own admission already validates
        context freshness per event, so gating audio dispatch on the
        *latest* ack only adds latency (e.g. right at heat start, when
        state changes are frequent) without a correctness benefit. But the
        server must have received *some* state and a clock mapping for
        this session at least once (_handshake_complete), or every event
        is rejected as stale_context / needing a clock exchange -- which
        tears the connection down and is far worse than the wait.
        """
        return bool(
            not self._closed
            and self._session
            and self._state
            and self._handshake_complete
        )

    def can_command(self) -> bool:
        """Allow one operator action at a time on a connected source."""
        return self._ready() and (self._command_task is None or self._command_task.dead)

    def command(self, operation: str) -> bool:
        """Queue a captured manual action, including immediately after a stop update."""
        if (
            self._closed
            or not self._session
            or self._state is None
            or (self._command_task is not None and not self._command_task.dead)
            or operation not in {"prepare", "clear_cache"}
        ):
            return False
        self._command_sequence += 1
        data = {
            "version": "race-events/1",
            "session_id": self._session,
            "context": copy.deepcopy(self._state["context"]),
            "settings_revision": self._state["context"]["revision"],
            "command_id": f"{self._session}:command:{self._command_sequence}",
            "operation": operation,
        }
        self.command_status = f"{operation}: queued"
        self._command_task = gevent.spawn(
            self._run_command, data, self._serial, self._url
        )
        return True

    def _run_command(self, data: dict, serial: int, url: str) -> None:
        try:
            posted = False
            while not self._closed:
                if (
                    serial != self._serial
                    or self._state is None
                    or data["context"] != self._state["context"]
                ):
                    self.command_status = "Cache command interrupted by state change"
                    return
                if not self._ready():
                    gevent.sleep(0.05)
                    continue
                if not posted:
                    response = self._commands.request(url, "POST", "/v2/commands", data)
                    posted = True
                else:
                    response = self._commands.request(
                        url, "GET", f"/v2/commands/{data['command_id']}"
                    )
                result = _checked(*response)
                status = result["status"]
                progress = (
                    f" ({result['completed']}/{result['total']})"
                    if "completed" in result and "total" in result
                    else ""
                )
                self.command_status = f"{data['operation']}: {status}{progress}"
                if status in {"completed", "cancelled", "failed"}:
                    return
                gevent.sleep(0.5)
        except Exception as err:
            self.command_status = f"Cache command result unknown: {err}"
            logger.warning("Race Voice %s", self.command_status)
        finally:
            self._commands.close()

    def _disconnect(self, status: str) -> None:
        self._serial += 1
        self._session = None
        self._ack_revision = 0
        self._handshake_complete = False
        self._pending.clear()
        self._set_status(status)
        self._wake.set()

    def _set_status(self, status: str) -> None:
        if status != self.status:
            self.status = status
            logger.info("Race Voice event service: %s", status)

    def _request(
        self, url: str, method: str, path: str, data: dict | None = None
    ) -> dict:
        status, result = self._control.request(url, method, path, data)
        return _checked(status, result)

    def _connect(self, url: str) -> str:
        """Probe reachability before session negotiation; any healthy service is v2.

        A matching *epoch* (this publisher reconnecting, e.g. after its own
        restart) reclaims the existing session via the GET alone, with no
        POST and no conflict. A different epoch (a genuinely different
        publisher) still fences here; that's left for an operator to
        resolve rather than silently overridden.
        """
        self._request(url, "GET", "/health")
        owner = self._request(url, "GET", "/v2/session")
        if owner.get("epoch") != self._epoch:
            owner = self._request(
                url,
                "POST",
                "/v2/session",
                {
                    **owner,
                    "epoch": self._epoch,
                    "nonce": uuid.uuid4().hex,
                    "takeover": False,
                },
            )
        session = owner.get("session_id")
        if not isinstance(session, str) or not session:
            raise ValueError("Service did not return a session")
        return session

    def _synchronize(self) -> None:
        serial, url = self._serial, self._url
        if self._session is None:
            session = self._connect(url)
            if serial != self._serial:
                return
            if session != self._last_session:
                self._sequence = next_counter(0)
                self._command_sequence = next_counter(0)
            self._session = self._last_session = session
            self._clock_due = 0
        state = self._state
        session = self._session
        if self._ack_revision != state["context"]["revision"]:
            self._request(url, "PUT", "/v2/state", {**state, "session_id": session})
            if serial != self._serial or state is not self._state:
                return
            self._ack_revision = state["context"]["revision"]
        if time.monotonic() >= self._clock_due:
            self._ack_revision = 0
            probe = self._request(
                url,
                "POST",
                "/v2/clock",
                {"session_id": session, "sent": time.monotonic()},
            )
            received = time.monotonic()
            self._request(
                url,
                "POST",
                "/v2/clock",
                {
                    "session_id": session,
                    "probe_id": probe["probe_id"],
                    "received": received,
                },
            )
            if serial != self._serial or state is not self._state:
                return
            self._clock_due = time.monotonic() + 20
            self._ack_revision = state["context"]["revision"]
            self._handshake_complete = True
        self._set_status("Connected")
        self._audio_wake.set()

    def _run_control(self) -> None:
        try:
            while not self._closed:
                timeout = (
                    max(0, self._clock_due - time.monotonic()) if self._ready() else 20
                )
                self._wake.wait(timeout=timeout)
                self._wake.clear()
                if self._state is None:
                    continue
                try:
                    self._synchronize()
                except Exception as err:
                    self._disconnect(str(err))
                    gevent.sleep(1)
        finally:
            self._control.close()

    def _run_audio(self) -> None:
        try:
            while not self._closed:
                self._audio_wake.wait()
                self._audio_wake.clear()
                while self._pending and self.can_send_audio():
                    event = min(self._pending, key=_priority)
                    self._pending.remove(event)
                    if event["expires_at"] <= time.monotonic():
                        continue
                    self._sequence += 1
                    session, serial = self._session, self._serial
                    event.update(
                        session_id=session,
                        sequence=self._sequence,
                        event_id=f"{session}:{self._sequence}",
                    )
                    try:
                        status, result = self._audio.request(
                            self._url, "POST", "/v2/events", event
                        )
                        if status != 429:
                            _checked(status, result)
                    except Exception as err:
                        if (
                            serial == self._serial
                            and event["context"] == self._state["context"]
                        ):
                            self._disconnect(str(err))
        finally:
            self._audio.close()

    def close(self) -> None:
        """Wait for senders to close their own connections during shutdown."""
        self._closed = True
        self._pending.clear()
        tasks = self._tasks + (
            [self._command_task] if self._command_task is not None else []
        )
        gevent.killall(tasks, block=True, timeout=3)
        if any(not task.dead for task in tasks):
            raise RuntimeError("Event senders did not finish shutdown")


def _priority(event: dict) -> int:
    if event["kind"] == "tone" and event["payload"].get("asset") == "audio_check":
        return 1
    if event["kind"] in {"tone", "countdown"}:
        return 0
    if event["kind"] == "lap":
        return 3
    return 1 if event["payload"].get("winner_flag") else 2


def _checked(status: int, result: dict) -> dict:
    if status >= 400:
        error = result.get("error", result.get("outcome", "request failed"))
        message = f"Service HTTP {status}: {error}"
        raise ValueError(message)
    return result
