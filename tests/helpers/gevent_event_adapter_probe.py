"""Run RH adapter scenarios in a fresh, gevent-patched interpreter."""

# Patch before importing anything that can retain native sockets or locks.
# ruff: noqa: E402, PT009, PT027, SLF001
from gevent import monkey

monkey.patch_all()

import json
import os
import sys
import time
import unittest
import uuid
from types import SimpleNamespace
from unittest.mock import Mock

import gevent
from gevent.event import Event
from gevent.pywsgi import WSGIServer

# RH modules are supplied by the host in production. No Piper/ONNX stubs are used.
_EVENTS = [
    "STARTUP",
    "SHUTDOWN",
    "HEAT_SET",
    "OPTION_SET",
    "RACE_STAGE_TONE",
    "RACE_START",
    "RACE_CLOCK_CALLOUT",
    "RACE_SCHEDULE",
    "RACE_SCHEDULE_CANCEL",
    "PILOT_ADD",
    "PILOT_ALTER",
    "PILOT_DELETE",
    "HEAT_ALTER",
    "HEAT_DELETE",
    "DATABASE_RESET",
    "DATABASE_IMPORT",
    "DATABASE_RESTORE",
    "DATABASE_RECOVER",
    "DATABASE_INITIALIZE",
]
sys.modules["eventmanager"] = SimpleNamespace(
    Evt=SimpleNamespace(**{name: name for name in _EVENTS})
)
sys.modules["filtermanager"] = SimpleNamespace(
    Flt=SimpleNamespace(EMIT_PHONETIC_DATA="lap", EMIT_PHONETIC_TEXT="voice")
)
sys.modules["RHUI"] = Mock()
sys.modules["flask"] = Mock()
os.environ["RACE_VOICE_EXPERIMENTAL_EVENTS"] = "1"

from custom_plugins.race_voice import const, initialize
from custom_plugins.race_voice.event_output import JsonChannel


def wait_for(predicate) -> None:  # noqa: ANN001
    """Yield to senders until an observable result or bounded failure."""
    with gevent.Timeout(5, AssertionError("Adapter did not reach expected state")):
        while not predicate():
            gevent.sleep(0.005)


class Service:
    """Record real HTTP requests and independently delay control or audio replies."""

    def __init__(self) -> None:
        """Bind only an ephemeral local test port."""
        self.owner = {
            "boot_id": uuid.uuid4().hex,
            "owner_revision": 0,
            "epoch": "",
            "nonce": "",
            "session_id": None,
        }
        self.state = None
        self.sequence = 0
        self.events = []
        self.requests = []
        self.audio_entered = Event()
        self.audio_release = Event()
        self.audio_release.set()
        self.health_entered = Event()
        self.health_release = Event()
        self.health_release.set()
        self.fail_event = False
        self.server = WSGIServer(("127.0.0.1", 0), self.handle, log=None)
        self.server.start()
        self.url = f"http://127.0.0.1:{self.server.server_port}"

    def handle(self, environ: dict, start_response) -> list[bytes]:  # noqa: ANN001
        """Respond with minimal protocol state while exercising actual sockets."""
        path = environ["PATH_INFO"]
        length = int(environ.get("CONTENT_LENGTH") or 0)
        data = json.loads(environ["wsgi.input"].read(length)) if length else {}
        self.requests.append((path, data))
        status, result = self.dispatch(environ["REQUEST_METHOD"], path, data)
        body = b"x" * 70_000 if path == "/huge" else json.dumps(result).encode()
        start_response(
            f"{status} Test",
            [("Content-Type", "application/json"), ("Content-Length", str(len(body)))],
        )
        return [body]

    def dispatch(self, method: str, path: str, data: dict) -> tuple[int, dict]:  # noqa: C901, PLR0911
        """Retain sequence high-water marks when a publisher resumes its session."""
        if path == "/health":
            self.health_entered.set()
            self.health_release.wait()
            return 200, {"race_event_preview": True}
        if path == "/v2/session":
            if method == "POST":
                if (
                    self.owner["epoch"]
                    and self.owner["epoch"] != data["epoch"]
                    and not data.get("takeover")
                ):
                    return 409, {"error": "takeover required"}
                self.owner.update(
                    epoch=data["epoch"],
                    nonce=data["nonce"],
                    session_id=uuid.uuid4().hex,
                )
                self.owner["owner_revision"] += 1
                self.sequence = 0
            return 200, self.owner
        if path == "/v2/state":
            self.state = data
        if path == "/v2/clock" and "probe_id" not in data:
            return 200, {"probe_id": "clock-probe"}
        if path == "/v2/events":
            self.audio_entered.set()
            self.audio_release.wait()
            if data["context"] != self.state["context"]:
                return 409, {"outcome": "stale_context"}
            if data["sequence"] <= self.sequence:
                return 200, {"outcome": "duplicate"}
            self.sequence = data["sequence"]
            self.events.append(data)
            if self.fail_event:
                self.fail_event = False
                return 503, {"error": "response lost after admission"}
        return 200, {"outcome": "accepted"}

    def close(self) -> None:
        """Release every test request before shutting its server down."""
        self.audio_release.set()
        self.health_release.set()
        self.server.stop(timeout=0.1)


def make_adapter(url: str):  # noqa: ANN201
    """Keep every database read on the calling RH/test greenlet."""
    owner = gevent.getcurrent()
    options = {const.ENABLE_OPTION: True, const.SENDSPIN_SERVICE_URL_OPTION: url}
    pilot = SimpleNamespace(id=7, callsign="Alpha", phonetic="Alfa")
    rh = Mock()
    rh.race.heat = 1
    rh.race.start_time_internal = time.monotonic() + 1

    def owned(value):  # noqa: ANN001, ANN202
        if gevent.getcurrent() is not owner:
            raise AssertionError("Background sender accessed RH database")
        return value

    rh.db.option.side_effect = lambda name, default: owned(options.get(name, default))
    rh.db.option_set.side_effect = lambda name, value: options.update(
        {name: owned(value)}
    )
    rh.db.slots_by_heat.side_effect = lambda _heat: owned(
        [SimpleNamespace(pilot_id=7, node_index=0)]
    )
    rh.db.pilot_by_id.side_effect = lambda _pilot: owned(pilot)
    rh.db.heat_by_id.side_effect = lambda _heat: owned(SimpleNamespace(name="Heat one"))
    adapter = initialize(rh)
    return adapter, rh, options, pilot


class AdapterTests(unittest.TestCase):
    """Exercise queues, RH callbacks and connection recovery with gevent enabled."""

    def setUp(self) -> None:
        """Create one isolated source and service per scenario."""
        self.service = Service()
        self.addCleanup(self.service.close)
        self.adapter, self.rh, self.options, self.pilot = make_adapter(self.service.url)
        self.addCleanup(self.adapter.close)

    def connect(self) -> None:
        """Wait for real ownership, snapshot and clock requests to complete."""
        self.adapter._startup()
        wait_for(self.adapter._publisher._ready)

    def test_no_tts_imports_and_callbacks_only_capture_values(self) -> None:
        """Initialization selects the adapter before importing the legacy plugin."""
        self.assertFalse(self.service.requests)
        self.connect()
        payload = {
            "pilot_id": 7,
            "pilot": "Alfa",
            "lap": 3,
            "phonetic": "twenty three seconds",
        }
        self.assertIs(self.adapter._lap(payload), payload)
        payload["pilot"] = "Modified after callback"
        wait_for(lambda: len(self.service.events) == 1)
        self.assertEqual(self.service.events[0]["payload"]["pilot_name"], "Alfa")
        self.assertEqual(self.service.events[0]["payload"]["pilot_id"], 7)
        self.assertFalse(
            any(
                name == "piper"
                or name.startswith(("piper.", "onnxruntime"))
                or name == "custom_plugins.race_voice.plugin"
                for name in sys.modules
            )
        )
        self.assertFalse(self.rh.db.option_set.call_args_list == [])

    def test_stop_bypasses_blocked_audio_and_keeps_hub_responsive(self) -> None:
        """A delayed event reply cannot hold the latest control snapshot hostage."""
        self.connect()
        self.service.audio_release.clear()
        self.adapter._voice({"text": "Long request"})
        wait_for(self.service.audio_entered.is_set)
        ticks = []

        def heartbeat() -> None:
            for _ in range(10):
                ticks.append(time.monotonic())
                gevent.sleep(0.005)

        task = gevent.spawn(heartbeat)
        for lap in range(1, 101):
            self.adapter._lap(
                {
                    "pilot_id": lap % 6 + 1,
                    "pilot": "Pilot",
                    "lap": lap,
                    "phonetic": "one second",
                }
            )
        self.assertLessEqual(len(self.adapter._publisher._pending), 4)
        self.adapter.stop_audio()
        generation = self.adapter._generation
        wait_for(lambda: self.service.state["context"]["generation"] == generation)
        self.assertFalse(self.service.audio_release.is_set())
        self.assertFalse(self.adapter._publisher._pending)
        task.join()
        self.assertEqual(len(ticks), 10)
        self.service.audio_release.set()
        wait_for(self.adapter._publisher._ready)
        self.adapter._stage({"scheduled_at_monotonic": time.monotonic() + 1})
        wait_for(lambda: len(self.service.events) == 1)
        self.assertEqual(self.service.events[0]["kind"], "tone")

    def test_queue_limits_latest_pilot_and_signal_priority(self) -> None:
        """Retain only current laps, cap announcements and make room for beeps."""
        self.connect()
        publisher = self.adapter._publisher
        for lap in range(1, 51):
            self.adapter._lap(
                {"pilot_id": 7, "pilot": "Alfa", "lap": lap, "phonetic": "one second"}
            )
        self.assertEqual(len(publisher._pending), 1)
        self.assertEqual(publisher._pending[0]["payload"]["lap"], 50)
        for _ in range(50):
            self.adapter._voice({"text": "Announcement"})
        self.assertEqual(len(publisher._pending), 33)
        self.adapter._stage({"scheduled_at_monotonic": time.monotonic() + 1})
        self.assertEqual(len(publisher._pending), 32)
        self.assertFalse(any(item["kind"] == "lap" for item in publisher._pending))
        wait_for(lambda: bool(self.service.events))
        self.assertEqual(self.service.events[0]["kind"], "tone")

    def test_reconnect_preserves_sequence_and_drops_disconnected_events(self) -> None:
        """Lost replies do not replay old audio or reset a live session's sequence."""
        self.connect()
        session = self.adapter._publisher._session
        self.service.fail_event = True
        self.adapter._voice({"text": "First"})
        wait_for(lambda: self.adapter._publisher._session is None)
        self.assertFalse(self.adapter._emit("voice", {"text": "During outage"}))
        wait_for(self.adapter._publisher._ready)
        self.assertEqual(session, self.adapter._publisher._session)
        self.adapter._voice({"text": "Second"})
        wait_for(lambda: len(self.service.events) == 2)
        self.assertEqual([item["sequence"] for item in self.service.events], [1, 2])

    def test_heat_settings_and_database_changes_publish_without_laps(self) -> None:
        """Roster changes retain identity; sound and database changes fence audio."""
        self.connect()
        competition = self.adapter._competition
        self.pilot.callsign = "New callsign"
        self.adapter._refresh()
        wait_for(self.adapter._publisher._ready)
        self.assertEqual(self.service.state["pilots"][0]["callsign"], "New callsign")
        self.assertEqual(self.adapter._generation, 0)
        self.options[const.SPEECH_SPEED_OPTION] = 1.2
        self.adapter._option_changed({"option": const.SPEECH_SPEED_OPTION})
        wait_for(self.adapter._publisher._ready)
        self.assertEqual(self.service.state["voice"]["speed"], 1.2)
        self.assertEqual(self.adapter._generation, 1)
        self.rh.race.heat = 2
        self.adapter._heat({})
        wait_for(self.adapter._publisher._ready)
        self.assertEqual(self.service.state["context"]["heat_id"], 2)
        self.adapter._reset({})
        wait_for(self.adapter._publisher._ready)
        self.assertNotEqual(
            competition, self.service.state["context"]["competition_id"]
        )
        self.assertEqual(self.service.state["pilots"][0]["pilot_id"], 7)

    def test_existing_owner_requires_manual_takeover(self) -> None:
        """A restart cannot silently replace an unrelated active publisher."""
        self.service.owner.update(
            epoch="previous-process", session_id="previous-session"
        )
        self.adapter._startup()
        wait_for(lambda: "takeover required" in self.adapter._publisher.status)
        self.assertEqual(self.service.owner["epoch"], "previous-process")
        self.adapter.connect()
        wait_for(self.adapter._publisher._ready)
        self.assertEqual(self.service.owner["epoch"], self.adapter._publisher._epoch)

    def test_url_change_during_handshake_does_not_install_old_session(self) -> None:
        """An in-flight handshake cannot attach the old session to a new URL."""
        other = Service()
        self.addCleanup(other.close)
        self.service.health_release.clear()
        self.adapter._startup()
        wait_for(self.service.health_entered.is_set)
        self.options[const.SENDSPIN_SERVICE_URL_OPTION] = other.url
        self.adapter._option_changed({"option": const.SENDSPIN_SERVICE_URL_OPTION})
        self.service.health_release.set()
        wait_for(self.adapter._publisher._ready)
        self.assertEqual(self.adapter._publisher._session, other.owner["session_id"])
        self.adapter._voice({"text": "New destination"})
        wait_for(lambda: bool(other.events))
        self.assertFalse(self.service.events)

    def test_supported_events_keep_phonetics_and_scheduled_targets(self) -> None:
        """Holeshots are silent, unassigned laps remain unassigned and clocks lead."""
        self.connect()
        self.adapter._lap({"lap": 0})
        self.assertFalse(self.adapter._publisher._pending)
        self.adapter._lap(
            {"lap": 1, "pilot_id": None, "pilot": "R1", "phonetic": "four seconds"}
        )
        wait_for(lambda: len(self.service.events) == 1)
        self.assertIsNone(self.service.events[0]["payload"]["pilot_id"])
        target = time.monotonic() + 1
        self.adapter._clock_callout(
            {"seconds_remaining": 30, "scheduled_at_monotonic": target}
        )
        wait_for(lambda: len(self.service.events) == 2)
        self.assertEqual(self.service.events[1]["kind"], "countdown")
        self.assertEqual(self.service.events[1]["play_at"], target)
        self.adapter._clock_callout(
            {"seconds_remaining": 5, "scheduled_at_monotonic": target}
        )
        wait_for(lambda: len(self.service.events) == 3)
        self.assertEqual(self.service.events[2]["payload"], {"asset": "stage"})
        self.assertEqual(self.service.events[2]["expires_at"], target + 0.25)

    def test_http_response_size_is_bounded_and_connection_recovers(self) -> None:
        """A bad service reply cannot allocate unbounded memory or poison reuse."""
        channel = JsonChannel("")
        self.addCleanup(channel.close)
        with self.assertRaisesRegex(ValueError, "64 KiB"):
            channel.request(self.service.url, "GET", "/huge")
        self.assertEqual(channel.request(self.service.url, "GET", "/health")[0], 200)

    def test_state_update_does_not_postpone_due_clock_refresh(self) -> None:
        """Frequent settings/roster updates must not age the service clock out."""
        self.connect()
        count = sum(path == "/v2/clock" for path, _ in self.service.requests)
        self.adapter._publisher._clock_due = time.monotonic() + 0.04
        self.adapter._refresh()
        wait_for(
            lambda: (
                sum(path == "/v2/clock" for path, _ in self.service.requests) > count
            )
        )

    def test_clock_refresh_retains_source_session(self) -> None:
        """Refresh clocks independently of synthesis and keep the same publisher."""
        self.connect()
        session = self.adapter._publisher._session
        self.adapter._publisher._clock_due = 0
        self.adapter._publisher._wake.set()
        wait_for(lambda: self.adapter._publisher._clock_due > time.monotonic())
        self.assertEqual(self.adapter._publisher._session, session)


def integration(url: str) -> None:
    """Use the actual aiohttp service supplied by the parent test process."""
    adapter, _rh, _options, _pilot = make_adapter(url)
    try:
        adapter._startup()
        wait_for(adapter._publisher._ready)
        adapter._lap(
            {"pilot_id": 7, "pilot": "Alfa", "lap": 3, "phonetic": "twenty seconds"}
        )
        gevent.sleep(0.15)
        adapter._stage({"scheduled_at_monotonic": time.monotonic() + 1})
        gevent.sleep(0.15)
        adapter.stop_audio()
        wait_for(adapter._publisher._ready)
        sys.stdout.write(
            json.dumps(
                {
                    "connected": adapter._publisher.status == "Connected",
                    "tts_imported": "piper" in sys.modules
                    or "onnxruntime" in sys.modules,
                }
            )
        )
    finally:
        adapter.close()


if __name__ == "__main__":
    if len(sys.argv) == 2:
        integration(sys.argv[1])
    else:
        result = unittest.TextTestRunner(stream=sys.stderr, verbosity=2).run(
            unittest.defaultTestLoader.loadTestsFromTestCase(AdapterTests)
        )
        raise SystemExit(not result.wasSuccessful())
