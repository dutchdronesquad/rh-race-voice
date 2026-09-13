"""Exercise service countdown timing with a controlled clock and timer queue."""

# ruff: noqa: PT009

from __future__ import annotations

import unittest
from unittest.mock import Mock, patch

from sendspin_service.race_schedule import RaceSchedule


class RaceScheduleTests(unittest.TestCase):
    """Run delayed callbacks explicitly, including callbacks cancelled after dequeue."""

    def setUp(self) -> None:
        """Use a publisher clock 100 seconds behind the service clock."""
        self.emit = Mock()
        self.schedule = RaceSchedule(self.emit)
        self.loop = Mock()
        self.enterContext(
            patch(
                "sendspin_service.race_schedule.asyncio.get_running_loop",
                return_value=self.loop,
            )
        )
        self.now = self.enterContext(
            patch("sendspin_service.race_schedule.time.monotonic", return_value=1000)
        )
        self.key = ("session", 1, 970)

    def fire(self, index: int) -> None:
        """Invoke an original queued callback even when its handle was cancelled."""
        args = self.loop.call_later.call_args_list[index].args
        args[1](*args[2:])

    def test_offset_and_thresholds_are_mapped_once_without_duplicates(self) -> None:
        """Map the planned RH time to four delays in the service clock."""
        self.schedule.update(self.key, 970, 100)
        self.assertEqual(
            [call.args[0] for call in self.loop.call_later.call_args_list],
            [10, 40, 60, 65],
        )
        self.fire(0)
        self.fire(0)
        self.emit.assert_called_once_with(60, 910)
        self.now.return_value = 1011
        self.schedule.update(self.key, 970, 101)
        self.assertEqual(self.loop.call_later.call_count, 7)
        self.fire(1)
        self.emit.assert_called_once()
        self.fire(4)
        self.assertEqual(self.emit.call_args.args, (30, 940))

    def test_refresh_keeps_an_already_scheduled_imminent_threshold(self) -> None:
        """A clock refresh just before a threshold must not discard its timer."""
        self.schedule.update(self.key, 970, 100)
        self.now.return_value = 1009.9
        self.schedule.update(self.key, 970, 100)
        self.assertAlmostEqual(self.loop.call_later.call_args_list[4].args[0], 0.1)
        self.fire(4)
        self.emit.assert_called_once_with(60, 910)

    def test_elapsed_thresholds_are_skipped_on_connect(self) -> None:
        """Reconnection never replays the earlier countdown backlog."""
        self.schedule.update(("session", 1, 930), 930, 100)
        self.assertEqual(
            [call.args[0] for call in self.loop.call_later.call_args_list], [20, 25]
        )

    def test_cancel_and_replacement_fence_dequeued_callbacks(self) -> None:
        """An old heat, stop or replaced schedule cannot revive a ready callback."""
        self.schedule.update(self.key, 970, 100)
        self.schedule.cancel()
        self.fire(0)
        self.emit.assert_not_called()
        self.schedule.update(("session", 2, 980), 980, 100)
        self.fire(1)
        self.emit.assert_not_called()
        self.fire(4)
        self.emit.assert_called_once_with(60, 920)
