"""Verify telemetry.record emits well-formed, correlatable JSON log lines."""

# ruff: noqa: PT009

from __future__ import annotations

import json
import unittest
from unittest.mock import patch

from sendspin_service.race import telemetry


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


if __name__ == "__main__":
    unittest.main()
