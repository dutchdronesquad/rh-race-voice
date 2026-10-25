# Race-audio latency correlation telemetry

Progress on #304, part of epic #296. This covers instrumentation and deterministic regression scenarios; hardware and audible validation remain outstanding. #304 stays open.

## What's implemented

`sendspin_service/race/telemetry.py` adds one dependency-free function, `record(event_id, stage, **fields)`, that logs a single-line JSON object (`event_id`, `stage`, a monotonic `t`, plus whatever fields the call site passes) through the `sendspin_service.telemetry` logger. It does nothing when INFO logging is not enabled for that logger, so it stays cheap on an idle service.

The `received` record includes event kind, pilot ID, lap number, heat ID and generation for HTTP events. Intermediate records cover admission, synthesis timing, cache hits, prefix publication and output scheduling. Final records are owned by preparation until audio is published, then by each configured output independently.

Before fan-out, a rejected, expired, cancelled or failed preparation produces one `output_dropped` without a destination. That global outcome covers the outputs that never received audio. After fan-out, each configured destination produces exactly one of `output_played`, `output_dropped` or `output_filtered`. Filtering has `reason=selection`; a broken filter has `reason=filter_failed`. A service with no outputs reports `no_destinations`. Output-local receipt state prevents a late sink reply, an interrupt and shutdown from reporting multiple final outcomes for the same admitted output. It is retained only with the bounded output item, not in a growing global event registry.

`output_played` is the existing stage name and now carries `delivery=queued`: the sink or relay accepted the audio. It does not confirm that a speaker produced sound. An output already marked queued can later have its buffered audio cleared; its original queue outcome is not rewritten. Preparation that published an early prefix leaves final accounting to the outputs, including when its time generation is cancelled or fails.

Pending expiry, invalidation, preemption, playback failure, stop-barrier failure and shutdown are explicitly recorded. Reasons include `expired`, `superseded`, `preempted`, `state_changed`, `heat_changed`, `competition_changed`, `voice_changed`, `session_changed`, `cache_clear`, `shutdown`, `synthesis_failed`, `synthesis_empty`, `synthesis_incomplete`, `playback_failed`, `stop_failed`, queue-capacity reasons and `sink`/`relay_error` when delivery did not succeed. `state_changed` covers a generation change whose wire snapshot does not distinguish an operator stop from a new staging sequence.

`tests/test_outcome_lifecycle.py` exercises pending and active cancellation, expiry pruning, shutdown, independent outputs and late replies. Existing HTTP burst and progressive-speech scenarios additionally verify that accepted work is accounted for under preparation or playback overload. These tests validate event handling and queue outcomes, not physical audio playback.

`tests/test_race_session.py` runs two complete six-round races, with four and eight pilots, through real HTTP admission, the production planners, speech planning, RAM reuse and a real synthesis-worker pipe. Only native voice generation and speaker output are replaced by controlled fakes. The scenarios start cold, explicitly prepare audio, then exercise warm prefixes, repeated bursts, explained overflow, a duplicate event, the finish buzzer and winner, heat changes, stop, expiry, a worker subprocess crash and recovery, a fresh staging signal and shutdown with work outstanding. A full output and an even-pilot selection are audited independently. Every accepted event must have exactly one global preparation failure or one final record per configured output; completed lap deliveries must contain all three segments without duplicate clips and preserve passage order. The RH adapter integration separately sends a `lap=None` finish payload before a winner and verifies that the invalid lap triggers no synthesis while the winner still reaches the service.

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

Remaining validation includes sustained hardware runs, audible output, Raspberry Pi capacity, player synchronization and buffering, numerical latency acceptance gates and mode-by-mode comparisons. Deterministic regressions do not establish gate-to-speaker latency or hardware acceptance. Worker, player and network failures still need broader combined coverage beyond the existing focused tests. No benchmark or production-service changes are needed to run the regression suite.
