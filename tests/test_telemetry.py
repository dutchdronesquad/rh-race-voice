"""Verify telemetry.record emits well-formed, correlatable JSON log lines."""

# ruff: noqa: PT009

from __future__ import annotations

import json
import unittest
from unittest.mock import patch

from sendspin_service import telemetry
from tools.latency_report import group_by_event, parse_line, percentile, summarize


class RecordTests(unittest.TestCase):
    """Check the on-the-wire shape callers depend on for correlation."""

    def test_record_logs_single_line_json_with_required_fields(self) -> None:
        """event_id, stage, t and extra fields all round-trip through logging."""
        with self.assertLogs("sendspin_service.telemetry", level="INFO") as captured:
            telemetry.record("session:1", "received", kind="lap", origin="event")
        self.assertEqual(len(captured.records), 1)
        message = captured.records[0].getMessage()
        self.assertNotIn("\n", message)
        payload = json.loads(message)
        self.assertEqual(payload["event_id"], "session:1")
        self.assertEqual(payload["stage"], "received")
        self.assertIsInstance(payload["t"], float)
        self.assertEqual(payload["kind"], "lap")
        self.assertEqual(payload["origin"], "event")

    def test_record_is_skipped_when_info_logging_is_disabled(self) -> None:
        """No log call is made when INFO is not enabled, keeping this cheap."""
        with (
            patch.object(telemetry.logger, "isEnabledFor", return_value=False),
            patch.object(telemetry.logger, "info") as info,
        ):
            telemetry.record("session:2", "received", kind="lap")
        info.assert_not_called()


class ReportAggregationTests(unittest.TestCase):
    """Exercise the pure parsing/aggregation functions used by the report tool."""

    def test_parse_line_extracts_embedded_json_and_skips_noise(self) -> None:
        """A telemetry line can be preceded by ordinary log formatting text."""
        prefixed = (
            "2026-09-14 12:00:00 INFO sendspin_service.telemetry "
            '{"event_id": "a", "stage": "received", "t": 1.0}'
        )
        record = parse_line(prefixed)
        self.assertEqual(record, {"event_id": "a", "stage": "received", "t": 1.0})
        self.assertIsNone(parse_line("not a telemetry line at all"))
        self.assertIsNone(parse_line("{}"))
        self.assertIsNone(parse_line('{"broken": '))

    def test_group_by_event_preserves_stage_order(self) -> None:
        """Records reconstruct one event's timeline in logged order."""
        records = [
            {"event_id": "a", "stage": "received", "t": 0.0},
            {"event_id": "b", "stage": "received", "t": 0.0},
            {"event_id": "a", "stage": "output_played", "t": 0.1},
        ]
        grouped = group_by_event(records)
        self.assertEqual(
            [r["stage"] for r in grouped["a"]], ["received", "output_played"]
        )
        self.assertEqual(len(grouped["b"]), 1)

    def test_percentile_uses_nearest_rank(self) -> None:
        """Percentiles are computed without any third-party numeric dependency."""
        values = [10.0, 20.0, 30.0, 40.0]
        self.assertEqual(percentile(values, 50), 20.0)
        self.assertEqual(percentile(values, 99), 40.0)
        self.assertIsNone(percentile([], 50))

    def test_summarize_computes_latency_and_drop_breakdown(self) -> None:
        """One played and one dropped event produce distinct kind statistics."""
        records = [
            {"event_id": "a", "stage": "received", "t": 0.0, "kind": "lap"},
            {"event_id": "a", "stage": "synthesis_start", "t": 0.1},
            {"event_id": "a", "stage": "synthesis_end", "t": 0.3},
            {"event_id": "a", "stage": "output_scheduled", "t": 0.31},
            {"event_id": "a", "stage": "output_played", "t": 0.5},
            {"event_id": "b", "stage": "received", "t": 1.0, "kind": "lap"},
            {
                "event_id": "b",
                "stage": "output_dropped",
                "t": 1.05,
                "reason": "no_room",
            },
        ]
        summaries = summarize(records)
        summary = summaries["lap"]
        self.assertEqual(summary.received, 2)
        self.assertEqual(summary.played, 1)
        self.assertEqual(summary.dropped, 1)
        self.assertEqual(summary.drop_reasons["no_room"], 1)
        self.assertAlmostEqual(summary.wall_latencies_ms[0], 500.0)
        self.assertAlmostEqual(summary.synthesis_latencies_ms[0], 200.0)

    def test_summarize_uses_non_accepted_admission_as_a_drop_reason(self) -> None:
        """An admission outcome other than accepted is a drop reason on its own."""
        records = [
            {"event_id": "c", "stage": "received", "t": 0.0, "kind": "voice"},
            {"event_id": "c", "stage": "admission", "t": 0.01, "outcome": "duplicate"},
        ]
        summary = summarize(records)["voice"]
        self.assertEqual(summary.received, 1)
        self.assertEqual(summary.played, 0)
        self.assertEqual(summary.dropped, 1)
        self.assertEqual(summary.drop_reasons["duplicate"], 1)


if __name__ == "__main__":
    unittest.main()
