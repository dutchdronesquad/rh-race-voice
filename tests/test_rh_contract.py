"""Run upstream RH contracts outside the asyncio test interpreter."""

# ruff: noqa: PT009

from __future__ import annotations

import json
import os
import subprocess
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PIN = json.loads((ROOT / "tests/rotorhazard.json").read_text())


def rotorhazard_source() -> Path | None:
    """Require the pinned source or the exact revision resolved by daily CI."""
    source = os.environ.get("RACE_VOICE_RH_SOURCE")
    if not source:
        return None
    root = Path(source).resolve()
    target = os.environ.get("RACE_VOICE_RH_TARGET", "development")
    expected = (
        os.environ.get("RACE_VOICE_RH_REVISION")
        or PIN["revisions"][target].rsplit("@", 1)[1]
    )
    revision = subprocess.check_output(  # noqa: S603
        ["git", "-C", str(root), "rev-parse", "HEAD"],  # noqa: S607
        text=True,
    ).strip()
    if revision != expected:
        message = f"Expected RH {target} {expected}, found {revision}"
        raise AssertionError(message)
    changes = subprocess.check_output(  # noqa: S603
        ["git", "-C", str(root), "status", "--porcelain", "--", "src"],  # noqa: S607
        text=True,
    )
    if changes:
        raise AssertionError("RotorHazard contract source must be unmodified")
    return root


class RotorHazardContractTests(unittest.TestCase):
    """A dedicated CI job always supplies the pinned source and dependencies."""

    def test_real_rhapi_and_phonetic_filters(self) -> None:
        """Run gevent and actual upstream modules in an isolated subprocess."""
        source = rotorhazard_source()
        if source is None:
            self.skipTest("Set RACE_VOICE_RH_SOURCE; see CONTRIBUTING.md")
        result = subprocess.run(  # noqa: S603
            [sys.executable, "-m", "tests.helpers.rh_contract_probe", str(source)],
            cwd=ROOT,
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("Ran 4 tests", result.stderr)
