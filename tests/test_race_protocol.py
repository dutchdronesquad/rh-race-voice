"""Conformance scenarios for race state fencing and cross-host deadlines."""

# ruff: noqa: PT009, PT027

from __future__ import annotations

import copy
import unittest
from dataclasses import replace

from sendspin_service.race_protocol import (
    VERSION,
    Admission,
    ClockMapping,
    ContextGate,
    EventKind,
    ProtocolError,
    RaceEvent,
)


def event_payload(sequence: int = 1) -> dict:
    """Describe an RH lap without depending on its plugin or synthesis imports."""
    return {
        "version": VERSION,
        "session_id": "session-a",
        "event_id": f"session-a:{sequence}",
        "sequence": sequence,
        "context": {
            "competition_id": "competition-a",
            "revision": 1,
            "generation": 0,
            "heat_id": 3,
        },
        "kind": "lap",
        "occurred_at": 100.0,
        "expires_at": 110.0,
        "payload": {
            "pilot_id": 7,
            "pilot_name": "Klaas",
            "lap": 4,
            "text": "twenty three point four five",
        },
    }


class EventParsingTests(unittest.TestCase):
    """Keep identity and exact supplied phonetic text independent of playback."""

    def test_preserves_spoken_text_and_separate_pilot_id(self) -> None:
        """Pilot routing must not interpret the callsign as a database key."""
        event = RaceEvent.parse(event_payload())
        self.assertEqual(event.pilot_id, 7)
        self.assertEqual(event.pilot_name, "Klaas")
        self.assertEqual(event.text, "twenty three point four five")

    def test_unassigned_pilot_remains_unassigned(self) -> None:
        """RH can announce a frequency for a gate without an assigned pilot."""
        data = event_payload()
        data["payload"]["pilot_id"] = None
        self.assertIsNone(RaceEvent.parse(data).pilot_id)

    def test_tone_requires_no_speech_text(self) -> None:
        """Bundled signals must be actionable without a TTS request."""
        data = event_payload()
        data.update(kind="tone", payload={"asset": "stage"}, play_at=101.0)
        event = RaceEvent.parse(data)
        self.assertEqual(event.kind, EventKind.TONE)
        self.assertIsNone(event.text)
        self.assertEqual(event.asset, "stage")

    def test_unknown_versions_assets_and_event_ids_are_rejected(self) -> None:
        """No silent acceptance of unsupported or ambiguous semantics."""
        for changes in (
            {"version": "race-events/2"},
            {"kind": "surprise"},
            {"kind": "tone", "payload": {"asset": "../../private"}},
            {"event_id": "session-a:2"},
            {"sequence": True},
        ):
            with self.subTest(changes=changes), self.assertRaises(ProtocolError):
                RaceEvent.parse(event_payload() | changes)

    def test_invalid_clock_values_do_not_enter_scheduler(self) -> None:
        """Reject NaN, infinity, booleans and nonsensical lifetimes."""
        for changes in (
            {"occurred_at": float("nan")},
            {"expires_at": float("inf")},
            {"play_at": True},
            {"expires_at": 100.0},
            {"expires_at": 4000.0},
            {"play_at": 110.0},
        ):
            with self.subTest(changes=changes), self.assertRaises(ProtocolError):
                RaceEvent.parse(event_payload() | changes)

    def test_bad_pilot_lap_and_context_types_are_rejected(self) -> None:
        """Do not coerce node names or booleans into identities."""
        for section, key, value in (
            ("payload", "pilot_id", "7"),
            ("payload", "lap", 0),
            ("payload", "text", ""),
            ("context", "heat_id", False),
            ("context", "revision", 0),
            ("context", "generation", -1),
        ):
            data = copy.deepcopy(event_payload())
            data[section][key] = value
            with self.subTest(key=key), self.assertRaises(ProtocolError):
                RaceEvent.parse(data)


class StateFencingTests(unittest.TestCase):
    """Exercise recovery using immutable events held across state transitions."""

    def setUp(self) -> None:
        """Install the initial authoritative context."""
        self.event = RaceEvent.parse(event_payload())
        self.gate = ContextGate("session-a")
        self.gate.snapshot("session-a", self.event.context)

    def test_lost_acknowledgement_does_not_replay_audio(self) -> None:
        """Retrying an accepted event is harmless without an unbounded ID set."""
        self.assertEqual(self.gate.admit(self.event, 110, 100), Admission.ACCEPTED)
        self.assertEqual(self.gate.admit(self.event, 110, 101), Admission.DUPLICATE)

    def test_out_of_order_audio_is_not_replayed(self) -> None:
        """The serialized sender can omit dropped laps; late packets stay dropped."""
        newest = RaceEvent.parse(event_payload(5))
        self.assertEqual(self.gate.admit(newest, 110, 100), Admission.ACCEPTED)
        self.assertEqual(self.gate.admit(self.event, 110, 100), Admission.DUPLICATE)

    def test_expired_retry_cannot_get_a_fresh_budget(self) -> None:
        """Consume expired sequences even if a later caller gives another deadline."""
        self.assertEqual(self.gate.admit(self.event, 99, 100), Admission.EXPIRED)
        self.assertEqual(self.gate.admit(self.event, 110, 100), Admission.DUPLICATE)

    def test_event_ahead_of_snapshot_can_retry_after_sync(self) -> None:
        """State/control can travel independently of audio admission."""
        current = replace(self.event.context, revision=2, generation=1)
        future = replace(self.event, context=current)
        self.assertEqual(self.gate.admit(future, 110, 100), Admission.NEED_SNAPSHOT)
        self.gate.snapshot("session-a", current)
        self.assertEqual(self.gate.admit(future, 110, 100), Admission.ACCEPTED)

    def test_stop_invalidates_late_worker_and_delayed_event(self) -> None:
        """A stop is authoritative even when a synthesis result arrives later."""
        self.gate.admit(self.event, 110, 100)
        self.gate.snapshot(
            "session-a", replace(self.event.context, revision=2, generation=1)
        )
        self.assertFalse(self.gate.is_current(self.event))
        delayed = RaceEvent.parse(event_payload(2))
        self.assertEqual(self.gate.admit(delayed, 110, 100), Admission.STALE_CONTEXT)

    def test_older_snapshot_cannot_undo_stop(self) -> None:
        """Reordered state delivery cannot reenable invalidated jobs."""
        stopped = replace(self.event.context, revision=3, generation=1)
        self.gate.snapshot("session-a", stopped)
        self.assertEqual(
            self.gate.snapshot("session-a", self.event.context), Admission.STALE_CONTEXT
        )
        self.assertFalse(self.gate.is_current(self.event))

    def test_heat_and_database_changes_require_invalidation(self) -> None:
        """Numeric pilot reuse cannot make old speech belong to a new competition."""
        for changes in ({"heat_id": 9}, {"competition_id": "new-database"}):
            bad = replace(self.event.context, revision=2, **changes)
            self.assertEqual(
                self.gate.snapshot("session-a", bad), Admission.STALE_CONTEXT
            )
        new_context = replace(
            self.event.context, competition_id="new-database", revision=2, generation=1
        )
        self.assertEqual(
            self.gate.snapshot("session-a", new_context), Admission.ACCEPTED
        )
        self.assertFalse(self.gate.is_current(self.event))

    def test_new_session_fences_old_requests_after_restart(self) -> None:
        """A valid old credential/session payload cannot replay after takeover."""
        gate = ContextGate("new-session")
        self.assertEqual(
            gate.snapshot("session-a", self.event.context), Admission.STALE_SESSION
        )
        self.assertEqual(gate.admit(self.event, 110, 100), Admission.STALE_SESSION)

    def test_display_update_keeps_already_admitted_audio(self) -> None:
        """A harmless roster revision need not interrupt a current callout."""
        self.gate.snapshot("session-a", replace(self.event.context, revision=2))
        self.assertTrue(self.gate.is_current(self.event))

    def test_initial_event_requires_snapshot(self) -> None:
        """Reconnect cannot play before authoritative context has been restored."""
        gate = ContextGate("session-a")
        self.assertEqual(gate.admit(self.event, 110, 100), Admission.NEED_SNAPSHOT)


class ClockMappingTests(unittest.TestCase):
    """Make transport delay consume the event budget across clock domains."""

    def test_offset_bounds_contain_asymmetric_network_delay(self) -> None:
        """A 1000-second clock offset does not become 1000 seconds of playback lag."""
        mapping = ClockMapping.from_exchange(10, 1010.02, 1010.025, 10.105)
        lower, upper = mapping.bounds(12, 1010.2)
        self.assertLessEqual(lower, 1012)
        self.assertGreaterEqual(upper, 1012)
        self.assertLessEqual(mapping.expiry(12, 1010.2), 1012)

    def test_jitter_increases_uncertainty(self) -> None:
        """A slow round trip may not be presented as precise scheduled timing."""
        fast = ClockMapping.from_exchange(10, 20.01, 20.01, 10.02)
        slow = ClockMapping.from_exchange(10, 20.01, 20.01, 10.8)
        self.assertGreater(slow.uncertainty, fast.uncertainty)

    def test_relay_does_not_restart_original_ttl(self) -> None:
        """Both hops map the remaining deadline, including elapsed queue time."""
        first = ClockMapping.from_exchange(100, 1100.01, 1100.01, 100.02)
        primary_deadline = first.expiry(105, 1100.1)
        second = ClockMapping.from_exchange(1103, 5103.01, 5103.01, 1103.02)
        remote_deadline = second.expiry(primary_deadline, 5103.1)
        self.assertLess(remote_deadline - 5103.1, 2)

    def test_stale_or_impossible_clock_exchange_is_rejected(self) -> None:
        """Refresh clocks after suspend instead of scheduling from stale data."""
        mapping = ClockMapping.from_exchange(1, 101, 101, 1.1)
        for now in (100, 132):
            with self.subTest(now=now), self.assertRaises(ProtocolError):
                mapping.bounds(5, now)
        with self.assertRaises(ProtocolError):
            ClockMapping.from_exchange(2, 100, 102, 2.1)
        with self.assertRaises(ProtocolError):
            ClockMapping.from_exchange(2, 100, 100, 1)


if __name__ == "__main__":
    unittest.main()
