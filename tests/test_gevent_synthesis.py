"""Run the RH monkey-patched environment separately from asyncio service tests."""

# ruff: noqa: PT009

from __future__ import annotations

import json
import subprocess
import sys
import unittest
from pathlib import Path


class GeventSynthesisTests(unittest.TestCase):
    """Check native work, cooperative waiting and RH callback thread affinity."""

    def test_synthesis_does_not_block_rh_heartbeat(self) -> None:
        """Exercise real cache/warmup orchestration with a blocking fake voice."""
        probe = Path(__file__).parent / "helpers/gevent_synthesis_probe.py"
        result = subprocess.run(  # noqa: S603
            [sys.executable, str(probe)],
            check=False,
            capture_output=True,
            text=True,
            timeout=30,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        report = json.loads(result.stdout)
        self.assertEqual(report["baseline_ticks"], 0)
        self.assertGreater(report["synthesis_ticks"], 0)
        self.assertGreater(report["warmup_ticks"], 0)
        self.assertGreater(report["load_ticks"], 0)
        self.assertTrue(report["native_work"])
        self.assertTrue(report["callbacks_on_hub"])
        self.assertTrue(report["cache_reused"])
        self.assertTrue(report["error_recovered"])
        self.assertEqual(report["max_concurrent"], 1)
