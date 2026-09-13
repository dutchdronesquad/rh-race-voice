# Race-aware service planning

Implementation for #300, with an opt-in local HTTP receiver for the #299 adapter work. Default deployments keep `/v1/play`. The preview connects source admission, the isolated Piper worker and one Sendspin output; the RH producer is the next integration step.

`PreparationPlanner` receives already-admitted, immutable events with their times mapped into the service clock. It keeps four pending laps plus one active speech job, supersedes pending laps for the same pilot, and bounds other announcements separately. Higher-priority speech can displace lower-priority pending work. Ready bundled tones bypass the synthesis worker entirely. Preempted inference results cannot reappear even if a dependency catches task cancellation.

`SpeechEngine` produces a complete tuple of immutable WAV bytes once. A routing callback can pass the same tuple to local/cloud and listener-specific outputs. Each `PlaybackPlanner` has its own bounded queue, stop barrier and sink, so a slow destination never owns a lock needed by another destination. Filtering must happen before the corresponding selection planner receives a lap.

Spoken countdowns and race beeps outrank other speech; general announcements outrank laps. Manual audio checks remain below race signals. Signals remove pending laps and cancel lower-priority active playback. The next callout waits until the sink has cleared its buffered audio. The sink must observe the cancellation flag before committing more PCM. Stop/reset ownership code must invalidate all preparation and output planners when changing generation, and wait for output clear before claiming that playback has stopped. Already audible sound cannot be recalled.

Output admission is bounded by count and retained audio bytes. Ordinary work leaves some byte capacity for signals. A higher-priority job can evict lower-priority pending work; an oversized/unusable job is rejected before interrupting playback. Queued laps favour newer speech for the same pilot. These limits are per output; the selection manager must separately bound the number of outputs.

`SendspinPlaybackSink` directly invokes the existing backend rather than sending the event through its legacy semantic queue again. WAV parsing and backend waits run off the planning loop. Targets, volume, cancellation and deadlines are retained. For scheduled stage/final-second tones, the final backend expiry is also capped at target + 250 ms; buzzers allow 1 second. This check runs against the backend's actual client lead calculation. The adapter must supply RH's advance target and a valid clock mapping; it cannot grant a fresh TTL after synthesis or relay transfer. These initial late-start bounds require hardware validation before activation.

Fourteen deterministic tests cover lap admission/replacement, ready tones during blocked inference, stop/state fencing, priority, byte/count limits, independent outputs, and the direct backend adapter's latest-start constraint. This is not an end-to-end cloud, client synchronization or WindowsSpin audio-quality measurement.

## Local HTTP preview

From a source checkout with the Python dependencies and Sendspin extra installed, run `python -m sendspin_service --experimental-race-cache-dir /path/to/race_voice_cache`. The directory contains `models/` and `tts/`. This explicitly selects event mode for the whole service: `/v1/play` and `/v1/stop` return 409, so an old producer cannot bypass the new scheduler. Omit the flag and restart to return to legacy mode. Do not point the current RH plugin at a preview instance; it still sends v1 audio.

The preview registers authenticated `/v2/session`, `/v2/state`, `/v2/clock` and `/v2/events` endpoints. It reports `race_event_preview: true` in health, rather than advertising the complete `race-events/1` capability. Requests are limited to 64 KiB, including chunked bodies. Binding the ingest API beyond localhost requires an API token; use TLS at the reverse proxy for remote access. The existing browser player and Sendspin client connection remain available.

A test producer follows this sequence:

1. `GET /v2/session` retrieves `boot_id` and `owner_revision`. `POST /v2/session` supplies those fields plus a process `epoch` and request `nonce`. Retrying the same request recovers the issued session; replacing a different epoch requires explicit `takeover: true` with the current revision. A service restart requires fresh discovery.
2. `PUT /v2/state` installs the complete snapshot from the [event contract](race-event-contract.md). For this preview, `voice.callout_flags` accepts `lap`, `voice`, `countdown` and `tone` booleans; omitted flags default to enabled. Changing any voice setting requires a higher generation. Reusing a revision with different snapshot content is rejected.
3. `POST /v2/clock` with `session_id` and publisher `sent` returns a `probe_id` and the service timestamps. Complete it with the same session, probe ID and publisher `received` timestamp. Probes expire after five seconds and can be used once; refresh the mapping within 30 seconds and after reconnect.
4. `POST /v2/events` sends the versioned event envelope. A 202 response means admission, and preparation can still expire or be superseded. Duplicate, expired and disabled events are terminal 200 outcomes. Stale context/session or missing state/clock returns 409; overload returns 429 and consumes that sequence. Drop expired work instead of granting it a fresh lifetime.
5. Stop by submitting a complete snapshot with increased revision and generation. The response waits for the Sendspin clear operation. If clearing fails, the service returns an error and refuses new events until a state retry successfully clears output. Sound already audible cannot be recalled.

Application cleanup stops both planners and reaps the child process. The worker starts on the first speech request, never on service construction. No automatic pre-cache runs. Manual prepare/clear commands, RH event publication, primary release packaging, cloud relay and personal selections remain separate follow-through work before normal activation.
