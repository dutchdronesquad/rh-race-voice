"""Prove the relay sink's content-addressed upload and fan-out isolation.

There is no real remote receiver yet (see race_relay.py's module docstring
and docs/race-event-contract.md) -- these tests stand in a small fake HTTP
server implementing the race-relay/1 routes, to pin down RaceRelaySink's own
wire behavior: cache-miss recovery, cancellation, and never raising on
ordinary delivery failure.
"""

# ruff: noqa: PT009, SLF001

from __future__ import annotations

import asyncio
import hashlib
import threading
import time
import unittest
import uuid
from dataclasses import replace

from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from sendspin_service.race.race_ingest import RaceIngest
from sendspin_service.race.race_planner import CalloutPlan, Destination
from sendspin_service.race.race_protocol import EventKind, RaceEvent
from sendspin_service.race.race_relay import RELAY_VERSION, RaceRelaySink
from tests.test_race_ingest import FakeSink, until
from tests.test_race_protocol import event_payload


def _plan(
    sequence: int,
    *,
    audio: tuple[bytes, ...] = (b"wav",),
    session_id: str = "session-a",
) -> CalloutPlan:
    """Build a plan directly, matching test_race_planner.py's helper."""
    event = replace(
        RaceEvent.parse(event_payload(sequence)),
        sequence=sequence,
        session_id=session_id,
        kind=EventKind.VOICE,
    )
    return CalloutPlan(event, time.monotonic() + 5, audio=audio, order=sequence)


class FakeRelayServer:
    """Implement the three race-relay/1 routes over an in-memory content store."""

    def __init__(self) -> None:
        """Record every request so tests can assert on delivery, not just outcome."""
        self.assets_requests: list[dict] = []
        self.uploads: list[str] = []
        self.events: list[dict] = []
        self.contexts: list[dict] = []
        self.headers: list[str] = []
        self.content: dict[str, bytes] = {}
        self.fail_events = False
        self.fail_events_times = 0
        self.upload_gate: asyncio.Event | None = None
        self.upload_entered = asyncio.Event()
        self._probe: dict | None = None

    def app(self) -> web.Application:
        """Build the aiohttp app implementing the race-relay/1 routes."""
        app = web.Application()
        app.router.add_post("/v2/relay/assets", self._assets)
        app.router.add_put("/v2/relay/assets/{content_id}", self._upload)
        app.router.add_post("/v2/relay/state", self._state)
        app.router.add_post("/v2/relay/clock", self._clock)
        app.router.add_post("/v2/relay/events", self._events)
        return app

    async def _state(self, request: web.Request) -> web.Response:
        body = await request.json()
        self.contexts.append(body)
        return web.json_response({"outcome": "accepted"})

    async def _clock(self, request: web.Request) -> web.Response:
        body = await request.json()
        now = time.monotonic()
        if "probe_id" not in body:
            self._probe = {
                "probe_id": uuid.uuid4().hex,
                "sent": body["sent"],
                "received_remote": now,
                "sent_remote": time.monotonic(),
            }
            return web.json_response(self._probe)
        self._probe = None
        return web.json_response({"offset": 0.0, "uncertainty": 0.01})

    async def _assets(self, request: web.Request) -> web.Response:
        self.headers.append(request.headers.get("Authorization", ""))
        body = await request.json()
        self.assets_requests.append(body)
        missing = [
            ref["sha256"] for ref in body["refs"] if ref["sha256"] not in self.content
        ]
        return web.json_response({"missing": missing})

    async def _upload(self, request: web.Request) -> web.Response:
        self.upload_entered.set()
        if self.upload_gate is not None:
            await self.upload_gate.wait()
        content_id = request.match_info["content_id"]
        self.content[content_id] = await request.read()
        self.uploads.append(content_id)
        return web.json_response({})

    async def _events(self, request: web.Request) -> web.Response:
        if self.fail_events or self.fail_events_times > 0:
            self.fail_events_times = max(0, self.fail_events_times - 1)
            return web.json_response({"error": "unavailable"}, status=503)
        self.events.append(await request.json())
        return web.json_response({})


class RaceRelaySinkTests(unittest.IsolatedAsyncioTestCase):
    """Exercise RaceRelaySink against the fake receiver above."""

    async def asyncSetUp(self) -> None:
        """Stand up the fake receiver and a sink pointed at it."""
        self.server = FakeRelayServer()
        self.client = TestClient(TestServer(self.server.app()))
        await self.client.start_server()
        self.addAsyncCleanup(self.client.close)
        base_url = str(self.client.make_url(""))
        self.sink = RaceRelaySink(base_url, token="relay-secret")  # noqa: S106
        self.addAsyncCleanup(self.sink.aclose)

    async def test_cache_miss_then_hit_uploads_once(self) -> None:
        """A repeated identical segment is announced but never re-uploaded."""
        await self.sink.play(_plan(1, audio=(b"same-audio",)), threading.Event())
        await self.sink.play(_plan(2, audio=(b"same-audio",)), threading.Event())
        content_id = hashlib.sha256(b"same-audio").hexdigest()
        self.assertEqual(self.server.uploads, [content_id])
        self.assertEqual(len(self.server.events), 2)
        self.assertEqual(
            self.server.events[0]["audio_refs"], self.server.events[1]["audio_refs"]
        )
        self.assertEqual(self.server.events[0]["version"], RELAY_VERSION)
        self.assertEqual(self.server.assets_requests[0]["version"], RELAY_VERSION)

    async def test_cancellation_before_upload_skips_delivery(self) -> None:
        """A plan cancelled up front never reaches the network."""
        cancelled = threading.Event()
        cancelled.set()
        await self.sink.play(_plan(1), cancelled)
        self.assertEqual(self.server.assets_requests, [])
        self.assertEqual(self.server.events, [])

    async def test_cancellation_during_upload_skips_the_event(self) -> None:
        """A plan cancelled mid-upload never sends the trailing event."""
        self.server.upload_gate = asyncio.Event()
        cancelled = threading.Event()

        async def cancel_once_uploading() -> None:
            async with asyncio.timeout(2):
                await self.server.upload_entered.wait()
            cancelled.set()
            self.server.upload_gate.set()

        task = asyncio.ensure_future(cancel_once_uploading())
        await self.sink.play(_plan(1, audio=(b"gated",)), cancelled)
        await task
        self.assertEqual(self.server.events, [])

    async def test_ordinary_relay_failures_do_not_raise(self) -> None:
        """A failing remote is logged and dropped, never raised."""
        self.server.fail_events = True
        with self.assertLogs("sendspin_service.race.race_relay", level="WARNING"):
            await self.sink.play(_plan(1), threading.Event())

    async def test_unreachable_host_does_not_raise(self) -> None:
        """A connection failure is treated the same as an HTTP error."""
        sink = RaceRelaySink("http://127.0.0.1:1", timeout_s=0.2)
        self.addAsyncCleanup(sink.aclose)
        with self.assertLogs("sendspin_service.race.race_relay", level="WARNING"):
            await sink.play(_plan(1), threading.Event())

    async def test_bearer_token_is_sent(self) -> None:
        """The configured relay token, not the ingest token, reaches the remote."""
        await self.sink.play(_plan(1), threading.Event())
        self.assertTrue(all(h == "Bearer relay-secret" for h in self.server.headers))

    async def test_stop_is_a_safe_no_op(self) -> None:
        """Stop does nothing yet; propagating it to a real remote is a follow-up."""
        await self.sink.stop()

    async def test_retry_recovers_from_a_transient_failure(self) -> None:
        """Two failures followed by a success still deliver the event."""
        self.server.fail_events_times = 2
        await self.sink.play(_plan(1), threading.Event())
        self.assertEqual(len(self.server.events), 1)

    async def test_retry_exhausted_never_raises(self) -> None:
        """A permanently failing remote drops the event after all attempts."""
        self.server.fail_events = True
        with self.assertLogs("sendspin_service.race.race_relay", level="WARNING"):
            await self.sink.play(_plan(1), threading.Event())
        self.assertEqual(self.server.events, [])

    async def test_push_context_sends_the_current_context(self) -> None:
        """push_context reaches the remote's state route."""
        context = _plan(1).event.context
        await self.sink.push_context(context)
        self.assertEqual(len(self.server.contexts), 1)
        sent = self.server.contexts[0]["context"]
        self.assertEqual(sent["competition_id"], context.competition_id)

    async def test_push_context_failure_does_not_raise(self) -> None:
        """A failing remote never breaks the caller's stop/generation-bump flow."""
        sink = RaceRelaySink("http://127.0.0.1:1", timeout_s=0.2)
        self.addAsyncCleanup(sink.aclose)
        with self.assertLogs("sendspin_service.race.race_relay", level="WARNING"):
            await sink.push_context(_plan(1).event.context)


class RaceRelayFanOutTests(unittest.IsolatedAsyncioTestCase):
    """A slow/backed-up relay destination cannot delay or starve local playback."""

    async def asyncSetUp(self) -> None:
        """Wire a permanently-hung relay destination alongside a working local one."""
        self.server = FakeRelayServer()
        self.server.upload_gate = asyncio.Event()  # never released -> relay hangs
        self.client = TestClient(TestServer(self.server.app()))
        await self.client.start_server()
        self.addAsyncCleanup(self.client.close)
        self.relay_sink = RaceRelaySink(str(self.client.make_url("")))
        self.addAsyncCleanup(self.relay_sink.aclose)
        self.local_sink = FakeSink()
        self.ingest = RaceIngest(
            _Worker(),
            {
                "local": Destination(self.local_sink),
                "relay": Destination(self.relay_sink),
            },
            {},
        )
        self.addAsyncCleanup(self.ingest.close)
        owner = self.ingest.owner()
        opened = await self.ingest.session(
            {**owner, "epoch": "rh-process", "nonce": "request-1"}
        )
        state = await self.ingest.state(
            {
                "version": "race-events/1",
                "session_id": opened["session_id"],
                "context": dict(event_payload()["context"]),
                "heat_name": "Heat 1",
                "pilots": [],
                "voice": {
                    "model": "en_GB-alan-medium",
                    "speed": 1,
                    "noise": 0.667,
                    "noise_w": 0.8,
                    "enabled": True,
                    "callout_flags": {},
                },
            }
        )
        self.assertEqual(state["outcome"], "accepted")
        self.session_id = opened["session_id"]

    async def test_relay_destination_does_not_starve_local(self) -> None:
        """The local destination plays promptly while the relay stays stuck."""
        plan = _plan(1, audio=(b"blocks-forever",), session_id=self.session_id)
        self.assertTrue(self.ingest._outputs["relay"].submit(plan))
        self.assertTrue(self.ingest._outputs["local"].submit(plan))
        await until(lambda: len(self.local_sink.played) == 1)
        self.assertEqual(len(self.local_sink.played), 1)


class _Worker:
    """Unused synthesis worker double; these tests submit CalloutPlans directly."""

    async def request(self, payload: dict, **_kwargs: object) -> dict:
        raise NotImplementedError

    async def close(self) -> None:
        """Satisfy RaceIngest.close(); nothing was ever started."""
        return
