# Race-audio latency correlation telemetry

Progress on #304, part of epic #296. This is instrumentation and one deterministic scenario, not the end-to-end validation the issue asks for. #304 stays open; see "What remains outstanding" below.

## What's implemented

`sendspin_service/telemetry.py` adds one dependency-free function, `record(event_id, stage, **fields)`, that logs a single-line JSON object (`event_id`, `stage`, a monotonic `t`, plus whatever fields the call site passes) through the standard `sendspin_service.telemetry` logger. It does nothing when INFO logging is not enabled for that logger, so it stays cheap on an idle service.

`record()` is called additively, alongside the existing log lines, at every point identified in the issue's scope: HTTP event receipt and internally scheduled countdown receipt (`received`, with `origin` distinguishing the two and no shared HTTP moment for schedule-originated events), admission outcome (`admission`), the disabled-callout-flag path (`disabled`), the two admission-time 429 paths (`dropped`), per-segment worker cache hit/duration (`segment`, via a new optional `on_segment` callback on `SpeechEngine.synthesize()`), synthesis start/end (`synthesis_start`/`synthesis_end`), every bounded-queue admission and eviction in both `PreparationPlanner` and `PlaybackPlanner` (`output_dropped`, with a `reason` and which `planner` dropped it), and the sink's final expiry/cancellation check and completed playback (`output_dropped`/`output_played`). None of this changes control flow, return values or existing log lines.

`tools/latency_report.py` reads a log file (path argument, or stdin) that mixes ordinary log output with `telemetry.record` lines, tolerates and skips anything that isn't one JSON record, groups records by `event_id`, and reports per-`kind` (as seen on `received`) event counts, drop counts with a reason breakdown, and p50/p95/p99 of `received -> output_played` wall latency and `synthesis_start -> synthesis_end` synthesis latency, in milliseconds. Percentiles use plain nearest-rank arithmetic over a sorted list; no numpy/pandas. Its parsing and aggregation functions are plain, separately importable functions so they can be unit-tested without shelling out.

`tests/test_telemetry.py` covers `record()`'s log shape and the report tool's parsing/percentile/aggregation functions directly. `tests/test_latency_scenario.py` is a deterministic **dense lap burst** scenario: it drives a real `RaceIngest` over real HTTP (session, state, clock exchange, `/v2/events`) with a fake synthesis worker held open by an `asyncio.Event`, submits lap events for more pilots than the bounded preparation queue can hold at once, and asserts against the captured telemetry log that every received event ends with exactly one terminal record (played, or a drop/disabled/non-accepted-admission record) and that no event is ever marked played twice. This is meant to read as the harness template for the other deterministic scenarios the issue lists, not as a complete scenario suite.

A known, deliberately unfixed gap: an event whose in-flight synthesis is cancelled by task cancellation (a higher-priority signal preempting an active lap) can lose its `synthesis_end`/terminal record if the cancellation lands inside the `await` in `PreparationPlanner._prepare()`, since Python does not run code after an awaited call that raised `CancelledError`. The dense-lap-burst scenario does not exercise this path (lap-on-lap admission never preempts), so it is not covered by a test. Handling it correctly needs a `try`/`except`/`finally` around that await, which was left out here to keep the wiring additive and boring; treat it as a follow-up alongside the fault-injection scenarios below. Likewise, drops caused by `PreparationPlanner.invalidate()`/`PlaybackPlanner.invalidate()` (state change, stop, heat change discarding pending/active work) are not yet instrumented.

## Capturing a baseline

Attach a `logging.FileHandler` to the `sendspin_service.telemetry` logger (at `INFO` or lower) before running a race, for example:

```python
import logging

handler = logging.FileHandler("race-telemetry.jsonl")
logging.getLogger("sendspin_service.telemetry").addHandler(handler)
logging.getLogger("sendspin_service.telemetry").setLevel(logging.INFO)
```

Run the race, then summarize the file:

```console
python -m tools.latency_report race-telemetry.jsonl
```

or pipe the service's combined log output directly into it over stdin; non-telemetry lines are ignored.

## What remains outstanding

This PR builds correlation instrumentation, a report tool and one scenario. Per the issue's full scope, the following are not done and cannot be claimed:

- Fault injection: worker crashes/hangs, primary restarts, cloud latency/outage, missing assets, duplicate/reordered events, clock offset, and slow/disconnected players.
- The other deterministic/replayable scenarios: pilots changing heats, database replacement, manual preparation, stop, late joins, selection changes, and expired work. The dense lap burst here is a template for these, not a substitute.
- RH heartbeat/chart responsiveness, CPU, memory, bandwidth, active selections and queue-depth sampling.
- Numerical p50/p95/p99 acceptance gates chosen from a recorded baseline, and a reproducible before/after report comparing them.
- Local-only, local+cloud, cloud-only, and separate-LAN-primary mode runs.
- The bundled browser player and WindowsSpin 2.2.6 audible tests on actual Raspberry Pi 4 hardware; simulated playback in these tests cannot establish beep quality or real audible latency.
- A sustained race-like run at documented listener/selection limits, comparing default playback and independent selections, identifying bottlenecks and publishing conservative operating limits.
- Confirming no stale replay, no duplicate synthesis from fan-out, bounded resources, local continuity during cloud outages, and acceptable RH responsiveness under load, plus the integrated #295 experience and packaging path required for release sign-off.

Implementation dependencies #301, #302, #303 and #295 are not yet merged, so end-to-end acceptance cannot be established yet regardless of instrumentation readiness. Hardware details (Pi RAM, OS/architecture, CPU throttling, model, network, client mode) remain unrecorded until the target Raspberry Pi 4 is available.
