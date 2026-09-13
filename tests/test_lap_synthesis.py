"""Exercise bounded lap work with a deterministic shared executor."""

# ruff: noqa: PT009

from __future__ import annotations

import unittest
from collections import deque
from unittest.mock import Mock, patch

from tests.test_plugin_startup import plugin_module


class LapSynthesisTests(unittest.TestCase):
    """Protect freshness and leave executor capacity for other announcements."""

    def setUp(self) -> None:
        """Record submitted callbacks instead of starting timing-sensitive threads."""
        self.tasks = deque()
        self.executor = Mock()
        self.executor.submit.side_effect = self.tasks.append
        self.play = Mock()
        self.queue = plugin_module.LapSynthesisQueue(self.executor, self.play)
        self.enterContext(patch("time.monotonic", return_value=10.0))

    def submit(self, pilot: int, expiry: float = 20.0) -> None:
        """Offer a minimal lap snapshot."""
        self.queue.submit({"pilot": pilot, "expires_at": expiry})

    def drain(self) -> None:
        """Run every deferred callback in submission order."""
        while self.tasks:
            self.tasks.popleft()()

    def test_burst_keeps_only_four_newest_pending_laps(self) -> None:
        """Do not fill the executor queue for every incoming lap."""
        for pilot in range(100):
            self.submit(pilot)
        self.assertEqual(len(self.tasks), 1)
        self.drain()
        self.assertEqual(
            [c.args[0]["pilot"] for c in self.play.call_args_list], [96, 97, 98, 99]
        )

    def test_replaces_same_pilot_and_yields_between_laps(self) -> None:
        """Keep another pilot and let a winner task run before the next lap."""
        self.submit(1)
        self.submit(2)
        self.submit(1)
        winner = Mock()
        self.tasks.append(winner)
        self.tasks.popleft()()
        self.assertEqual(self.play.call_args.args[0]["pilot"], 2)
        self.tasks.popleft()()
        winner.assert_called_once()
        self.drain()
        self.assertEqual(self.play.call_args.args[0]["pilot"], 1)

    def test_expiry_and_failure_do_not_stall_new_work(self) -> None:
        """Expired jobs never synthesize and a failed job releases the scheduler."""
        self.submit(1, expiry=9.0)
        self.assertEqual(len(self.tasks), 0)
        self.submit(2)
        self.play.side_effect = RuntimeError("synthesis failed")
        with self.assertLogs(
            "race_voice_startup_test.services.lap_synthesis", level="ERROR"
        ):
            self.drain()
        self.play.side_effect = None
        self.submit(3)
        self.drain()
        self.assertEqual(self.play.call_args.args[0]["pilot"], 3)
