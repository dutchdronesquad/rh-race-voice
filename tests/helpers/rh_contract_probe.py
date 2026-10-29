"""Exercise real RotorHazard APIs; only storage/socket boundaries are doubles."""

# Patch before RH imports, in this subprocess only.
# ruff: noqa: E402, PT009, SLF001
from gevent import monkey

monkey.patch_all()

import json
import sys
import unittest
from contextlib import nullcontext
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import gevent
from flask import Flask
from gevent.fileobject import FileObjectPosix

source = Path(sys.argv.pop(1))
sys.path[:0] = [str(source / "src/server"), str(source / "src/interface")]

import FlaskAppObj

FlaskAppObj.set_flask_app(Flask(__name__))

# Match the server's import order to resolve RH's circular UI/results imports.
import Results  # noqa: F401
import RHAPI
import RHUI
from eventmanager import EventManager, Evt
from filtermanager import FilterManager, Flt

from custom_plugins.race_voice import const, initialize


def wait_for(predicate) -> None:  # noqa: ANN001
    """Yield to real RH event handlers until their observable result arrives."""
    with gevent.Timeout(5, AssertionError("RH callback did not complete")):
        while not predicate():
            gevent.sleep(0.001)


class Database:
    """Replace persistence, retaining real RHAPI database wrappers/defaults."""

    def __init__(self, url: str = "http://127.0.0.1:1") -> None:
        """Create four pilots whose callsigns differ from their phonetic names."""
        self.options = {
            const.ENABLE_OPTION: True,
            const.SENDSPIN_SERVICE_URL_OPTION: url,
            "MinLapSec": 10,
        }
        self.pilots = {
            i: SimpleNamespace(id=i, callsign=f"Pilot {i}", phonetic=f"Phonetic {i}")
            for i in range(1, 5)
        }

    def get_db_session_handle(self):  # noqa: ANN201
        """Supply the storage transaction boundary expected by RHAPI."""
        return nullcontext()

    def get_option(self, name, default=None):  # noqa: ANN001, ANN201
        """Read stored options using the caller's real RH field default."""
        return self.options.get(name, default)

    def get_optionInt(self, name, default=0):  # noqa: ANN001, ANN201, N802
        """Match RH's integer option storage boundary."""
        return int(self.get_option(name, default))

    def set_option(self, name, value) -> None:  # noqa: ANN001
        """Retain adapter identity across API calls."""
        self.options[name] = value

    def get_pilot(self, pilot_id):  # noqa: ANN001, ANN201
        """Return a stored pilot record."""
        return self.pilots.get(pilot_id)

    def get_heat(self, _heat_id):  # noqa: ANN001, ANN201
        """Return the current stored heat."""
        return SimpleNamespace(name="Contract heat")

    def get_heatNodes_by_heat(self, _heat_id):  # noqa: ANN001, ANN201, N802
        """Return database slot records, leaving roster construction to the plugin."""
        return [SimpleNamespace(pilot_id=i, node_index=i - 1) for i in self.pilots]


class Runtime:
    """Load the plugin through real RHAPI, UI, events and filter registration."""

    def __init__(self, url: str = "http://127.0.0.1:1") -> None:
        """Provide database/configuration and browser emission boundaries only."""
        self.app = Flask("rh_contract")
        self.db = Database(url)
        self.socket = Mock()
        config = SimpleNamespace(
            migrations=[],
            get_item=lambda section, _name: (
                0 if section == "LOGGING" else "{m} {s}.{d}"
            ),
        )
        self.context = SimpleNamespace(
            rhdata=self.db,
            serverconfig=config,
            race=SimpleNamespace(current_heat=1),
        )
        self.context.events = EventManager(self.context)
        # Upstream keeps these on the class; each fixture gets its own registry.
        self.context.events.events = {}
        self.context.events.eventOrder = {}
        self.api = RHAPI.RHAPI(self.context)
        self.context.filters = FilterManager(self.api)
        self.context.rhui = RHUI.RHUI(
            self.app, self.socket, self.context, self.context.events
        )
        self.adapter = initialize(self.api)

    def startup(self) -> None:
        """Dispatch through upstream's asynchronous event manager."""
        self.context.events.trigger(Evt.STARTUP)
        wait_for(lambda: self.adapter._snapshot is not None)

    def lap(self, pilot: int, lap: int | None = 1, milliseconds: float = 12345) -> None:
        """Have upstream build the complete filter payload, including raw units."""
        self.context.rhui.emit_phonetic_data(pilot, lap, milliseconds, "")


class ContractTests(unittest.TestCase):
    """Catch host API/schema drift without reimplementing the RH side of it."""

    def setUp(self) -> None:
        """Record only the transport handoff; RH modules stay unpatched."""
        factory = self.enterContext(
            patch("custom_plugins.race_voice.event_adapter.EventPublisher")
        )
        self.publisher = factory.return_value
        self.runtime = Runtime()
        self.addCleanup(self.runtime.adapter.close)
        self.runtime.startup()

    def test_real_registration_and_callsign_roster(self) -> None:
        """Actual UIField, Flask blueprint and RHAPI registration must accept us."""
        self.assertIn("race_voice_player", self.runtime.app.blueprints)
        self.assertIn(Flt.EMIT_PHONETIC_DATA, self.runtime.context.filters.filters)
        self.assertIn(Evt.RACE_CLOCK_CALLOUT, self.runtime.context.events.events)
        roster = self.runtime.adapter._snapshot["pilots"]
        self.assertEqual(
            [p["spoken_name"] for p in roster], [f"Pilot {i}" for i in range(1, 5)]
        )

    def test_four_pilots_preserve_callsign_and_millisecond_time(self) -> None:
        """Real RH emits distinct callsign/phonetic fields and millisecond times."""
        for pilot in range(1, 5):
            self.runtime.lap(pilot)
        calls = self.publisher.submit.call_args_list
        self.assertEqual(
            [c.args[1]["pilot_name"] for c in calls],
            [f"Pilot {i}" for i in range(1, 5)],
        )
        self.assertTrue(all(c.args[0] == "lap" for c in calls))
        payload = self.runtime.socket.emit.call_args.args[1]
        self.assertEqual(payload["raw_time"], 12345)
        self.assertEqual(calls[-1].args[1]["text"], payload["phonetic"])
        self.assertIsNotNone(payload)

    def test_invalid_lap_and_raw_minimum_leave_host_payload_intact(self) -> None:
        """Finished/hole-shot laps and subminimum times cannot enter TTS."""
        for lap, milliseconds in ((None, 12345), (0, 12345), (1, 9999.9)):
            with self.subTest(lap=lap, milliseconds=milliseconds):
                self.runtime.lap(1, lap, milliseconds)
                payload = self.runtime.socket.emit.call_args.args[1]
                self.assertEqual(payload["lap"], lap)
                self.assertEqual(payload["raw_time"], milliseconds)
                self.publisher.submit.assert_not_called()
        self.runtime.lap(1, 1, 10000)
        self.publisher.submit.assert_called_once()

    def test_normal_finish_keeps_winner_then_heat_fences_audio(self) -> None:
        """RACE_STOP followed by upstream winner text must retain its generation."""
        generation = self.runtime.adapter._generation
        self.runtime.context.events.trigger(Evt.RACE_STOP)
        self.runtime.context.rhui.emit_phonetic_text(
            "The winner is Pilot 1", winner_flag=True
        )
        call = self.publisher.submit.call_args
        self.assertEqual(call.args[0], "voice")
        self.assertTrue(call.args[1]["winner_flag"])
        self.assertEqual(self.runtime.adapter._generation, generation)
        self.runtime.context.events.trigger(Evt.HEAT_SET)
        wait_for(lambda: self.runtime.adapter._generation > generation)


def publish(url: str) -> None:
    """Serve parent-controlled race actions over the real plugin HTTP publisher."""
    runtime = Runtime(url)
    try:
        runtime.startup()
        wait_for(runtime.adapter._publisher._ready)
        sys.stdout.write(json.dumps({"ready": True}) + "\n")
        sys.stdout.flush()
        with FileObjectPosix(sys.stdin, "r", close=False) as commands:
            for line in commands:
                command = json.loads(line)
                action = command["action"]
                if action == "lap":
                    runtime.lap(
                        command["pilot"],
                        command.get("lap", 1),
                        command.get("milliseconds", 12345),
                    )
                elif action == "finish":
                    runtime.context.events.trigger(Evt.RACE_STOP)
                    runtime.context.rhui.emit_phonetic_text(
                        "The winner is Pilot 1", winner_flag=True
                    )
                elif action == "abort":
                    generation = runtime.adapter._generation
                    runtime.context.events.trigger(Evt.RACE_ABORT)
                    wait_for(
                        lambda previous=generation: (
                            runtime.adapter._generation > previous
                        )
                    )
                else:
                    message = f"Unknown test action: {action}"
                    raise ValueError(message)
                wait_for(lambda: not runtime.adapter._publisher._pending)
                sys.stdout.write(json.dumps({"action": action}) + "\n")
                sys.stdout.flush()
    finally:
        runtime.adapter.close()


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "--publish":
        publish(sys.argv[2])
    else:
        unittest.main()
