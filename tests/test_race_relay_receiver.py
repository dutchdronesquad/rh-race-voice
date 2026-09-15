"""Prove the relay receiver's routes, admission and wire-compatibility with the sender.

The centerpiece test wires the real, already-shipped RaceRelaySink (PR #333)
directly against a real RaceRelayReceiver over one HTTP server -- the
strongest proof the two independently-built sides actually interoperate.
"""

# ruff: noqa: PT009, PT027

from __future__ import annotations

import hashlib
import threading
import time
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from sendspin_service.playback.audio_cache import AudioCache
from sendspin_service.race.race_protocol import EventKind
from sendspin_service.race.race_relay import RaceRelaySink
from sendspin_service.race.race_relay_receiver import (
    RaceRelayReceiver,
    _parse_plan,
    add_routes,
)
from sendspin_service.server import SendspinService, ServiceConfig, _create_app
from tests.test_race_ingest import FakeSink
from tests.test_race_relay import _plan


class RaceRelayReceiverTests(unittest.IsolatedAsyncioTestCase):
    """Exercise RaceRelayReceiver's routes directly, without a network hop."""

    async def asyncSetUp(self) -> None:
        """Wire a receiver over a bounded cache and a FakeSink for assertions."""
        directory = self.enterContext(TemporaryDirectory())
        self.cache = AudioCache(Path(directory))
        self.sink = FakeSink()
        self.receiver = RaceRelayReceiver(self.cache, self.sink)
        self.addAsyncCleanup(self.receiver.close)

    def test_announce_reports_missing_then_not_missing_after_upload(self) -> None:
        """A freshly-announced hash is missing until uploaded."""
        digest = hashlib.sha256(b"data").hexdigest()
        refs = {"version": "race-relay/1", "refs": [{"sha256": digest, "size": 4}]}
        self.assertEqual(self.receiver.assets(refs)["missing"], [digest])
        self.receiver.upload(digest, b"data")
        self.assertEqual(self.receiver.assets(refs)["missing"], [])

    def test_upload_rejects_mismatched_content(self) -> None:
        """Content is hashed on arrival, not trusted from the URL alone."""
        digest = hashlib.sha256(b"expected").hexdigest()
        with self.assertRaises(ValueError):
            self.receiver.upload(digest, b"wrong")

    async def test_event_with_an_unresolved_reference_is_rejected(self) -> None:
        """An audio_refs entry never uploaded cannot be admitted."""
        body = {
            "version": "race-relay/1",
            "origin_event_id": "s:1",
            "context": {
                "competition_id": "c",
                "revision": 1,
                "generation": 0,
                "heat_id": None,
            },
            "kind": "tone",
            "deadline_wall": time.time() + 5,
            "target_wall": None,
            "volume": 1.0,
            "pilot_id": None,
            "text": None,
            "lap": None,
            "pilot_name": None,
            "asset": "stage",
            "winner": False,
            "audio_refs": [hashlib.sha256(b"never-uploaded").hexdigest()],
        }
        with self.assertRaises(ValueError):
            self.receiver.event(body)

    async def test_valid_event_reaches_the_local_sink(self) -> None:
        """A fully resolvable event is submitted to this host's own planner."""
        digest = hashlib.sha256(b"beep").hexdigest()
        self.receiver.upload(digest, b"beep")
        body = {
            "version": "race-relay/1",
            "origin_event_id": "s:1",
            "context": {
                "competition_id": "c",
                "revision": 1,
                "generation": 0,
                "heat_id": 3,
            },
            "kind": "lap",
            "deadline_wall": time.time() + 5,
            "target_wall": None,
            "volume": 0.5,
            "pilot_id": 7,
            "text": "twenty seconds",
            "lap": 2,
            "pilot_name": "Klaas",
            "asset": None,
            "winner": False,
            "audio_refs": [digest],
        }
        result = self.receiver.event(body)
        self.assertEqual(result["outcome"], "accepted")
        await self.sink.entered.wait()
        self.assertEqual(self.sink.played[0].audio, (b"beep",))
        self.assertEqual(self.sink.played[0].event.kind, EventKind.LAP)
        self.assertEqual(self.sink.played[0].event.pilot_id, 7)
        self.assertEqual(self.sink.played[0].volume, 0.5)


class ParsePlanTests(unittest.TestCase):
    """Verify the wall-clock-to-monotonic conversion and its horizon bound."""

    def test_deadline_wall_converts_to_a_monotonic_deadline_near_now(self) -> None:
        """A 3-second-out wall-clock deadline lands ~3 seconds out on this clock."""
        directory = self.enterContext(TemporaryDirectory())
        cache = AudioCache(Path(directory))
        digest = hashlib.sha256(b"x").hexdigest()
        cache.store(digest, b"x")
        body = {
            "version": "race-relay/1",
            "origin_event_id": "s:1",
            "context": {
                "competition_id": "c",
                "revision": 1,
                "generation": 0,
                "heat_id": None,
            },
            "kind": "tone",
            "deadline_wall": time.time() + 3,
            "target_wall": None,
            "audio_refs": [digest],
        }
        plan = _parse_plan(body, cache, 1)
        self.assertAlmostEqual(plan.deadline - time.monotonic(), 3, delta=0.5)


class RelayRoundTripTests(unittest.IsolatedAsyncioTestCase):
    """Wire the real sender against the real receiver over one HTTP server."""

    async def asyncSetUp(self) -> None:
        """Serve RaceRelayReceiver's routes and point a real RaceRelaySink at them."""
        directory = self.enterContext(TemporaryDirectory())
        cache = AudioCache(Path(directory))
        self.sink = FakeSink()
        receiver = RaceRelayReceiver(cache, self.sink)
        self.addAsyncCleanup(receiver.close)
        app = web.Application()
        add_routes(app, receiver)
        self.client = TestClient(TestServer(app))
        await self.client.start_server()
        self.addAsyncCleanup(self.client.close)
        self.relay_sink = RaceRelaySink(str(self.client.make_url("")))
        self.addAsyncCleanup(self.relay_sink.aclose)

    async def test_relayed_plan_plays_through_the_receiving_local_sink(self) -> None:
        """A real RaceRelaySink.play() call reaches the receiver's local output."""
        plan = _plan(1, audio=(b"stage-tone",))
        await self.relay_sink.play(plan, threading.Event())
        await self.sink.entered.wait()
        received = self.sink.played[0]
        self.assertEqual(received.audio, (b"stage-tone",))
        self.assertEqual(received.event.kind, plan.event.kind)
        self.assertEqual(received.event.pilot_id, plan.event.pilot_id)


def _config(**overrides: object) -> ServiceConfig:
    """Build a ServiceConfig for the receiver-config tests below."""
    return ServiceConfig(race_cache_dir=Path("cache"), **overrides)


class RelayReceiverConfigTests(unittest.TestCase):
    """Require opt-in and a relay token before serving relay routes."""

    def test_receiver_routes_are_absent_by_default(self) -> None:
        """A bare ServiceConfig() registers no /v2/relay/* routes."""
        with patch("sendspin_service.server.SendSpinServer"):
            service = SendspinService(_config())
            app = _create_app(service)
        paths = [resource.canonical for resource in app.router.resources()]
        self.assertNotIn("/v2/relay/events", paths)

    def test_receiver_requires_token_off_loopback(self) -> None:
        """An exposed, enabled receiver without a relay token fails fast."""
        with self.assertRaisesRegex(ValueError, "requires --relay-token"):
            SendspinService(
                _config(
                    api_host="0.0.0.0",  # noqa: S104
                    api_token="ingest-token",  # noqa: S106
                    relay_receiver_enabled=True,
                )
            )

    def test_receiver_routes_registered_when_enabled(self) -> None:
        """An enabled receiver with a relay token registers the relay routes."""
        with patch("sendspin_service.server.SendSpinServer"):
            service = SendspinService(
                _config(
                    relay_receiver_enabled=True,
                    relay_token="secret",  # noqa: S106
                )
            )
            app = _create_app(service)
        paths = [resource.canonical for resource in app.router.resources()]
        self.assertIn("/v2/relay/events", paths)
