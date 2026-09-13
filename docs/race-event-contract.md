# Race events v1: standalone service contract

Status: implementation contract for #297, part of epic #296. The existing `/v1/play` API remains operational. An explicit [local HTTP preview](service-audio-planner.md#local-http-preview) now wires admission, synthesis and playback. It does not advertise the complete capability: enable `race-events/1` only when adapter follow-through, manual commands and remaining integration checks are complete.

## Decision and boundaries

Keep the first working path small: one RH adapter sends events to one primary service, one persistent Piper worker generates audio, and one default Sendspin output plays it. Complete and measure that path before adding cloud relay routing or personal pilot selections. Those remain later epic steps; mixing speech and beeps is optional follow-up work.

Use concrete components and the existing HTTP and Sendspin libraries. Do not introduce a broker, generic routing framework, automatic failover or relay chains. Preparation and playback have separate queues because synthesis is shared while each output must be able to stop or stall independently. The new path should replace legacy scheduling when enabled, not pass through both schedulers.

Use bounded HTTP JSON requests over a persistent connection for race events and full state snapshots. Separate the state/control sender from disposable audio events so a slow audio request cannot hold a stop or heat change hostage. Both operate on a single destination-owned state machine. No message broker is needed. Use a separate binary/content-addressed asset API for primary-to-relay audio.

The primary owns phrase generation and caches. Each output destination owns its audio scheduling and client buffers. A tone references a bundled asset and never waits for the synthesis worker. General speech, countdown speech, and lap speech are distinct classes. Do not derive classification or pilot identity from text.

The small RH adapter captures immutable event and state values on the RH side. It never blocks for acknowledgement inside a race callback. Its sender retries only unexpired events in sequence and keeps a coalesced latest full snapshot. Use four pending laps initially, replacing the same pilot or oldest pending lap; bound nonlap events separately (32 initially). Control state uses its own slot. Manual jobs are also bounded and receive an explicit busy response, never a silent drop. These starting bounds are subject to #304 hardware measurements.

## API and ownership

The proposed endpoints are `POST /v2/session`, `PUT /v2/state`, `POST /v2/events`, `POST /v2/commands`, and `POST /v2/clock`. Capabilities and limits are published in `/health`. JSON bodies are limited to 64 KiB; roster entries to 256; text to 4096 characters. Apply limits before admission. Acknowledgement means admission, not synthesis completion or playback.

The service is configured for one authenticated race source. Persist a random `source_id` outside the RH database. Scope credentials to this source; a caller cannot select an arbitrary source in its body. Publisher/operator credentials are never delivered to a browser. Remote HTTP requires TLS. Listener-session controls will have separate endpoints/authorization and no source-level stop.

Opening a session performs an explicit compare-and-swap takeover of the service's current owner revision, using a fresh request nonce and the adapter's runtime epoch. The response includes a server-generated `session_id`, service boot ID, owner revision and the nonce. A delayed open request with an old owner revision cannot reclaim the source. Only one serialized owner may retry takeover. A lost response is recovered by retrieving current ownership with the same authenticated identity and nonce, not by creating a second owner. An unexpected competing live epoch returns a conflict requiring an operator-selected owner, not auto-takeover. Takeover and service restart clear pending/buffered audio, fence old requests, and require a full snapshot plus clock exchange before events are accepted.

Local and cloud outputs do not open independent producer sessions from the plugin. The primary alone publishes to its configured relay, with a fresh hop-local session and sequence. Relay mode cannot forward to another relay or accept browser controls over the original source. Preserve origin event/context IDs for correlation and duplicate prevention; translate only hop clock fields.

## Identity and state

| Field | Lifetime / meaning |
| --- | --- |
| `source_id` | Persistent installation identity, outside RH database |
| `competition_id` | Random UUID stored in an RH option; renewed on destructive data changes |
| `epoch` | Random UUID per adapter process; never used as a saved player preference key |
| `session_id` | Destination-issued fence for this publisher connection ownership |
| `revision` | Strictly increasing full-state revision within the session |
| `generation` | Increases on stop, heat/context change, voice change or schedule cancellation |
| `sequence` | Strictly increasing audio event number within the session, never reset on stop |
| `event_id` | Exactly `session_id:sequence`; immutable across a retry |
| `origin_event_id` | Original source event identity retained across relays |

Full snapshots contain `context` (competition, revision, generation, nullable heat ID), heat display name, assigned pilots (`pilot_id`, callsign, spoken name, optional display name/colour/node), and voice settings (model, speed, noise, noise_w, enabled, callout flags). Model must be an allowed installed/downloadable voice; tuning values must pass the same finite/range validation as RH controls. Never accept model filenames, cache paths or arbitrary download URLs from a client. Duplicate pilot IDs are invalid. Empty heats are valid. A snapshot is installed atomically, including invalidation before its acknowledgement. Publish roster changes without waiting for speech, and expose current state on player connect.

A newer complete revision can skip intermediate revisions. A lower revision or same revision with different content is rejected. Identity/heat changes require a greater generation. A roster display edit may retain generation; pending jobs already admitted retain their captured phrase settings. A settings change that affects sound increments generation. A snapshot with lower generation is invalid. Admission requires an exact current context, while worker completion checks session/competition/heat/generation (a harmless display revision need not cancel already admitted speech). Implementations must also compare complete snapshot content when detecting a retry; the reference `ContextGate` handles context only.

### RotorHazard identity evidence and reset policy

The local RH source exposes `rhapi.db.option/option_set`, `Evt.DATABASE_RESET`, `DATABASE_IMPORT`, `DATABASE_RESTORE`, `DATABASE_RECOVER`, `DATABASE_INITIALIZE`, and pilot/heat mutation events. `DataImportManager.run_import` emits `DATABASE_IMPORT` after successful import without specifying whether pilot identity was replaced. Therefore conservatively renew `competition_id` on each successful import, restore, recovery, initialization, reset, or deletion of a pilot, and explain the selection reset. Refresh the roster for pilot/heat edits. Ordinary process restarts preserve the option. A missing option creates a new ID.

Store pilot selections under `(source_id, competition_id)`, never under node number or callsign. A heat change retains selected pilot IDs. A competition change resets to all pilots with a visible message. A restored backup might contain an old UUID, so the restore callback must replace it before publishing state. An offline filesystem replacement with the exact same saved UUID cannot be detected by that option alone. After manually replacing the database, the operator must use an explicit "new competition" reset before reusing saved pilot selections. Do not add filesystem identity tracking to infer these replacements.

## Event message

Machine-readable wire shapes are in [`protocol/race-events-v1.schema.json`](protocol/race-events-v1.schema.json). The stateful and cross-field rules below apply in addition to those shapes.

```json
{
  "version": "race-events/1",
  "session_id": "session-a",
  "event_id": "session-a:41",
  "sequence": 41,
  "context": {
    "competition_id": "competition-a",
    "revision": 12,
    "generation": 4,
    "heat_id": 3
  },
  "kind": "lap",
  "occurred_at": 800.0,
  "expires_at": 810.0,
  "payload": {
    "pilot_id": 7,
    "pilot_name": "Klaas",
    "lap": 4,
    "text": "twenty three point four five"
  }
}
```

`text` on a lap is the upstream phonetic time, not a locally reformatted numeric time. `pilot_name` is the upstream spoken name. Other voice events carry the complete phrase in `text`; voice events also preserve `winner_flag` as a boolean. Tones carry `asset: stage|buzzer|audio_check`, without a text/synthesis request. `play_at` optionally gives the target in the publisher's clock domain. Events have a positive lifetime of at most one hour and a scheduled target before expiry. NaN, infinities, boolean-as-number values, invalid types and unknown event kinds are rejected. Unknown optional envelope fields can be ignored in v1; unknown required semantics need a new advertised capability/version.

| RH source | Contract behaviour |
| --- | --- |
| `Flt.EMIT_PHONETIC_DATA` | Lap payload with explicit ID; skip holeshots as today |
| `Flt.EMIT_PHONETIC_TEXT` | Full upstream phrase and winner flag |
| `Evt.RACE_STAGE_TONE` | Stage asset and RH scheduled target |
| `Evt.RACE_START` | Buzzer at RH race start target |
| `Evt.RACE_CLOCK_CALLOUT` | Tone/buzzer or localized spoken countdown |
| `Evt.RACE_SCHEDULE` / cancel | Service countdown plan tied to RH target and generation |
| Heat/pilot/database changes | Atomic full snapshot and appropriate invalidation |
| Test phrase / audio check | Explicit voice / bundled asset request |
| Prepare / clear cache | Bounded manual commands with job status |
| Stop audio | Higher-generation snapshot immediately; never queued behind synthesis |

The inspected RH `emit_phonetic_data` payload contains `pilot_id`, while `pilot` is spoken text. Use `pilot_id` directly. For unassigned nodes RH emits a null ID and may supply the frequency as the spoken name: preserve those laps on the all-pilots programme only; never assign them to a selected pilot by name or node. Do not subscribe to both an underlying lap event and its phonetic filter as independent speakers. Scheduled countdown planning may derive announcement targets from the supplied RH schedule, but may not invent fallback stage/race-clock timers.

Manual commands contain version/session/context, a unique `command_id`, operation `prepare|clear_cache`, and an immutable settings revision. Deduplicate command IDs within the bounded job history; expired history returns "unknown", never silently reruns a destructive command. Return a job ID and expose queued/running/completed/failed/cancelled status. Cache clear increments audio generation, cancels work and uses safe cache ownership; a request cannot supply a filesystem path. Preparation is explicit, fills missing entries, and yields between phrases to live work.

## Ordering, retry and recovery

The audio sender serializes admission requests. Sequence gaps are allowed because disposable laps may be dropped before sending. Destination retains a high-water sequence rather than an unbounded set of seen IDs. A sequence at or below the watermark cannot enqueue again. A same sequence with different payload is a sender violation and never replays. Event ID is derived from the sequence, so reusing an ID at a higher sequence is malformed. Out-of-order audio is skipped, not reordered into historical playback. Full state uses a separate monotonic revision.

Outcomes: `accepted`, `duplicate`, `expired`, `stale_context`, `stale_session`, `need_snapshot`, `overloaded`. Invalid syntax returns 400; auth failure 401/403; session/context conflict 409; oversized body 413; overload 429 with a bounded retry hint. Expired/overloaded jobs consume the audio sequence. `need_snapshot` does not, so a current event can be retried after synchronization. Never automatically retry expired audio. An `accepted` job may later expire, be superseded or be interrupted.

Stop advances generation and snapshots it to every destination independently. On reconnect, install the latest snapshot before sending fresh work; discard disconnected audio backlog. A remote endpoint cannot receive a stop during a network outage, nor recall sound already audible. Short expiries limit damage. No automatic switch back to plugin synthesis when the primary disconnects.

## Clock mapping and deadlines

For each hop exchange four monotonic timestamps: publisher send `t1`, destination receive `t2`, destination reply `t3`, publisher receive `t4`. The publisher sends the completed sample with a server-issued, single-use probe ID; destination verifies its own `t2/t3`, boot/session and freshness. Never trust arbitrary claimed remote timestamps to bypass lateness. With nonnegative network delays, destination-minus-publisher offset lies in `[t3-t4, t2-t1]`. Midpoint is an estimate, half-width is uncertainty. Reject impossible exchanges, refresh at most every 30 seconds, include drift allowance (initially 100 ppm), and refresh after reconnect/suspend/clock jumps.

Translate expiry using the earliest possible deadline. Translate the target by the midpoint and record uncertainty. If uncertainty exceeds 100 ms for a scheduled tone, refresh or drop it by its deadline with a diagnostic; do not claim precision. Initial maximum late start is 250 ms for a stage/final-second beep and 1 second for a buzzer. A target can be scheduled only if client lead still fits this allowance and the event expiry; otherwise drop, rather than compressing missed countdown beeps together. These limits must be validated on hardware before release.

Convert scheduled target and expiry once at admission into destination monotonic time. Queue/synthesis waits consume that budget. A relay maps the already-consumed deadline/target into its outgoing clock, including accumulated uncertainty; it never grants the original TTL again. Finally translate destination monotonic time to the Sendspin clock with a local paired sample. Clock changes require remapping pending targets conservatively. Local/cloud clocks and video overlays need not share exact audible timing.

## Compatibility and rollout

Existing `/v1/play`, cache references, local/cloud output, and default Sendspin clients remain unchanged until the new path is enabled. An old plugin uses v1. A new plugin may use its explicit legacy mode with an old service during the transition, or reports a required-service-upgrade error for standalone mode. Never infer support from package version or silently dual-publish. A new primary can adapt audio to a legacy cloud relay using v1, but must report unavailable personal selection/context guarantees and must not advertise full relay support. Changing mode drains/fences the old producer before enabling the new one.

## Conformance and follow-through

`sendspin_service/race_protocol.py` provides strict event parsing, clock bounds, and a single-owner context gate. `race_ingest.py` connects these to the worker and planners in the local HTTP preview, validates full snapshot content and fences publisher sessions until service restart. Tests exercise duplicate delivery, old sessions, reordered snapshots, reset with reused pilot IDs, expiry, clock offsets/jitter, and cancellation of late worker results. Integration work must enforce the remaining limits before advertising the capability. #298–#304 own worker, adapter, planner, relay, selection, packaging and real hardware checks.
