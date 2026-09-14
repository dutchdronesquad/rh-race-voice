"""Summarize p50/p95/p99 latency and drop reasons from telemetry.record log lines.

Usage
-----
python -m tools.latency_report path/to/service.log
python -m tools.latency_report < service.log

The input is ordinary log output; only lines containing one JSON object with
`event_id`, `stage` and `t` (as produced by `sendspin_service.telemetry.record`)
are used, everything else is ignored.
"""

from __future__ import annotations

import argparse
import json
import math
import sys
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Iterable

REQUIRED_FIELDS = {"event_id", "stage", "t"}
DROP_STAGES = {"output_dropped", "dropped"}


def parse_line(line: str) -> dict | None:
    """Extract one telemetry record from a log line, skipping non-JSON output."""
    start = line.find("{")
    if start == -1:
        return None
    try:
        payload = json.loads(line[start:])
    except ValueError:
        return None
    if not isinstance(payload, dict) or not payload.keys() >= REQUIRED_FIELDS:
        return None
    return payload


def load_records(lines: Iterable[str]) -> list[dict]:
    """Parse every line, discarding anything that is not a telemetry record."""
    return [record for record in map(parse_line, lines) if record is not None]


def group_by_event(records: Iterable[dict]) -> dict[str, list[dict]]:
    """Reconstruct each event's stage timeline in the order records were logged."""
    grouped: dict[str, list[dict]] = defaultdict(list)
    for record in records:
        grouped[record["event_id"]].append(record)
    return grouped


def percentile(values: list[float], pct: float) -> float | None:
    """Return the nearest-rank percentile without a third-party dependency."""
    if not values:
        return None
    ordered = sorted(values)
    rank = min(len(ordered), max(1, math.ceil(pct / 100 * len(ordered))))
    return ordered[rank - 1]


@dataclass
class KindSummary:
    """Per-kind counts, drop breakdown and latency percentiles, in milliseconds."""

    kind: str
    received: int = 0
    played: int = 0
    dropped: int = 0
    drop_reasons: Counter = field(default_factory=Counter)
    wall_latencies_ms: list[float] = field(default_factory=list)
    synthesis_latencies_ms: list[float] = field(default_factory=list)


def _drop_reason(timeline: list[dict]) -> str | None:
    """Pick one representative reason describing why an event never played."""
    for record in timeline:
        stage = record["stage"]
        if stage in DROP_STAGES:
            return str(record.get("reason", stage))
        if stage == "admission" and record.get("outcome") not in (None, "accepted"):
            return str(record["outcome"])
        if stage == "disabled":
            return "disabled"
    return None


def summarize(records: Iterable[dict]) -> dict[str, KindSummary]:
    """Group telemetry records by event, then by the kind seen at "received"."""
    summaries: dict[str, KindSummary] = {}
    for timeline in group_by_event(records).values():
        received = next((r for r in timeline if r["stage"] == "received"), None)
        if received is None:
            continue
        kind = str(received.get("kind", "unknown"))
        summary = summaries.setdefault(kind, KindSummary(kind))
        summary.received += 1
        played = next((r for r in timeline if r["stage"] == "output_played"), None)
        if played is not None:
            summary.played += 1
            summary.wall_latencies_ms.append((played["t"] - received["t"]) * 1000)
        else:
            reason = _drop_reason(timeline)
            if reason is not None:
                summary.dropped += 1
                summary.drop_reasons[reason] += 1
        start = next((r for r in timeline if r["stage"] == "synthesis_start"), None)
        end = next((r for r in timeline if r["stage"] == "synthesis_end"), None)
        if start is not None and end is not None:
            summary.synthesis_latencies_ms.append((end["t"] - start["t"]) * 1000)
    return summaries


def _fmt(value: float | None) -> str:
    return "n/a" if value is None else f"{value:.1f}"


def format_report(summaries: dict[str, KindSummary]) -> str:
    """Render one plain-text block per event kind, sorted for stable output."""
    lines: list[str] = []
    for kind in sorted(summaries):
        summary = summaries[kind]
        lines.append(f"{kind}:")
        lines.append(f"  events: {summary.received}")
        lines.append(f"  played: {summary.played}")
        lines.append(f"  dropped: {summary.dropped}")
        for reason, count in summary.drop_reasons.most_common():
            lines.append(f"    {reason}: {count}")
        lines.append(
            "  wall latency ms (received -> output_played): "
            f"p50={_fmt(percentile(summary.wall_latencies_ms, 50))} "
            f"p95={_fmt(percentile(summary.wall_latencies_ms, 95))} "
            f"p99={_fmt(percentile(summary.wall_latencies_ms, 99))} "
            f"n={len(summary.wall_latencies_ms)}"
        )
        lines.append(
            "  synthesis latency ms (synthesis_start -> synthesis_end): "
            f"p50={_fmt(percentile(summary.synthesis_latencies_ms, 50))} "
            f"p95={_fmt(percentile(summary.synthesis_latencies_ms, 95))} "
            f"p99={_fmt(percentile(summary.synthesis_latencies_ms, 99))} "
            f"n={len(summary.synthesis_latencies_ms)}"
        )
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    """Print a latency/drop summary from one telemetry log file or stdin."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "path", nargs="?", type=Path, help="Telemetry log file; defaults to stdin"
    )
    args = parser.parse_args(argv)
    handle = args.path.open(encoding="utf-8") if args.path else sys.stdin
    try:
        records = load_records(handle)
    finally:
        if args.path:
            handle.close()
    summaries = summarize(records)
    if not summaries:
        print("No telemetry records found.")  # noqa: T201
        return 0
    print(format_report(summaries))  # noqa: T201
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
