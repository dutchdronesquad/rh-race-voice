# Race-audio latency correlation telemetry

Progress on #304, part of epic #296. This is instrumentation and one deterministic scenario, not the end-to-end validation the issue asks for. #304 stays open.

## What's implemented

`sendspin_service/race/telemetry.py` adds one dependency-free function, `record(event_id, stage, **fields)`, that logs a single-line JSON object (`event_id`, `stage`, a monotonic `t`, plus whatever fields the call site passes) through the `sendspin_service.telemetry` logger. It does nothing when INFO logging is not enabled for that logger, so it stays cheap on an idle service.

`record()` is called additively, alongside the existing log lines, at every point the issue calls out: HTTP event receipt and internally scheduled countdown receipt (`received`), admission outcome (`admission`), the disabled-callout-flag and 429 paths (`disabled`/`dropped`), per-segment worker cache hit/duration (`segment`, via a new optional `on_segment` callback on `SpeechEngine.synthesize()`), synthesis start/end (`synthesis_start`/`synthesis_end`), bounded-queue admission/eviction in both planners, and the sink's final expiry/cancellation check and completed playback (`output_dropped`/`output_played`). None of this changes control flow, return values or existing log lines. Each drop is recorded once, at the call site that actually decided it — `RaceIngest` does not also log a generic drop on top of the specific reason a planner already recorded. A synthesis task cancelled mid-`await` (a higher-priority signal preempting an active lap) still records a `cancelled` drop before the `CancelledError` propagates, and the sink only records `output_played` when the backend actually queued audio (`SendSpinServer.play()` returns `False` on a no-op — no connected clients, not ready, stream error — instead of silently doing nothing).

`tests/test_telemetry.py` covers `record()`'s log shape. `tests/test_latency_scenario.py` is a **dense lap burst** scenario: it drives a real `RaceIngest` over real HTTP with a fake synthesis worker held open, submits more pilots than the bounded preparation queue can hold at once, and asserts against the captured telemetry log that every received event ends with exactly one terminal record (played, dropped, disabled, or a non-accepted admission) and that no event is ever marked played twice. `tests/test_race_planner.py` separately covers the preemption-cancellation and sink no-op cases directly. This is meant as the template for the other deterministic scenarios the issue lists, not a complete scenario suite.

Known, deliberately unfixed gap: drops caused by `invalidate()` (state/stop/heat change discarding pending or active work) aren't instrumented yet.

There is deliberately no report/analysis tool yet — building one now would be guessing at a shape before there's a real telemetry log to look at. The output is one JSON object per line (`event_id`, `stage`, `t`, plus stage-specific fields), so a short ad-hoc script or `jq` is enough once there's an actual baseline to analyze.

## Capturing a baseline

Attach a `logging.FileHandler` to the `sendspin_service.telemetry` logger (at `INFO` or lower) before running a race:

```python
import logging

handler = logging.FileHandler("race-telemetry.jsonl")
logging.getLogger("sendspin_service.telemetry").addHandler(handler)
logging.getLogger("sendspin_service.telemetry").setLevel(logging.INFO)
```

## What remains outstanding

Everything else in the issue's scope: fault injection (worker crashes/hangs, primary restarts, cloud outage, missing assets, duplicate/reordered events, clock offset, slow/disconnected players), the other deterministic scenarios (heat changes, database replacement, manual preparation, stop, late joins, selection changes, expired work), RH/CPU/memory/bandwidth/queue-depth sampling, numerical p50/p95/p99 acceptance gates and a before/after report, the local/cloud/LAN-primary mode runs, real Raspberry Pi 4 / WindowsSpin 2.2.6 hardware and audible tests, and a sustained race-like run at documented listener limits.

Implementation dependencies #301, #302, #303 and #295 are not yet merged, so end-to-end acceptance cannot be established regardless of instrumentation readiness.
