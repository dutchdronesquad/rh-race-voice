# Architecture

## Runtime Flow

```text
RotorHazard event/filter
  -> RaceEventAdapter (custom_plugins/race_voice/event_adapter.py)
  -> EventPublisher (custom_plugins/race_voice/event_output.py, HTTP /v2/*)
  -> sendspin-service: RaceIngest (sendspin_service/race/race_ingest.py)
  -> PreparationPlanner -> SpeechEngine -> SynthesisWorker
       -> isolated synthesis_worker.py subprocess (sendspin_service/synthesis/)
  -> fan-out to named PlaybackPlanner destinations (sendspin_service/race/race_planner.py)
  -> SendspinPlaybackSink -> SendSpinServer (sendspin_service/playback/sendspin.py)
  -> Sendspin browser/player clients and WindowsSpin
```

The main RotorHazard sources are `Flt.EMIT_PHONETIC_DATA`, `Flt.EMIT_PHONETIC_TEXT`, `Evt.RACE_CLOCK_CALLOUT`, `Evt.RACE_STAGE`, `Evt.RACE_ABORT`, `Evt.HEAT_SET`, `Evt.RACE_SCHEDULE`/`Evt.RACE_SCHEDULE_CANCEL`, and the pilot/heat/database events that trigger a full state refresh.

The RotorHazard plugin is a bounded event/state adapter: it captures RH values on callbacks, builds a small JSON snapshot and disposable audio-event messages, and delivers them to `sendspin-service` over HTTP. It performs no synthesis and never imports Piper or ONNX. `sendspin-service` owns synthesis, the WAV cache, `aiosendspin`, player connections, stream state, and the Sendspin player endpoint on port `8927`. Staging tones and the race-start buzzer travel through the same `/v2/events` path as spoken callouts, carrying a bundled asset name instead of text. All of them are scheduled at once from `Evt.RACE_STAGE`, which fires once, several seconds ahead, already carrying every stage-tone time (`pi_staging_at_s` + `staging_tones`) and the start time (`pi_starts_at_s`) -- `Evt.RACE_STAGE_TONE` and `Evt.RACE_START` each fire too close to their own target to schedule a precisely-timed tone from (RACE_START in particular fires with no lead at all: RH busy-waits to the exact start instant before triggering it).

## Plugin Package

`custom_plugins/race_voice/`:

- `__init__.py`: the plugin entry point. `initialize()` constructs and returns a `RaceEventAdapter`; nothing else is instantiated at load time.
- `event_adapter.py`: `RaceEventAdapter` registers RH event/filter callbacks and the settings UI, builds the race-state snapshot (heat, roster, voice settings, generation/revision bookkeeping), performs local admission (enabled flag, text/length limits) before handing work to the publisher, and exposes the quick-button actions (test phrase, audio check, stop, connect/take-over, cache commands).
- `event_output.py`: `EventPublisher` and `JsonChannel`, a bounded, gevent-cooperative HTTP client that owns session negotiation (`/v2/session`), clock exchange (`/v2/clock`), state delivery (`/v2/state`), disposable audio-event delivery (`/v2/events`), and manual cache commands (`/v2/commands`, `/v2/commands/{command_id}`). It keeps one latest state and a small priority-ordered pending-event list; it performs no synthesis.
- `ui.py`: `register_ui()` registers the RotorHazard settings panel, options, and quick buttons, and serves the browser player at `/player` through a Flask blueprint backed by the `player/` directory (the Vite build output).
- `const.py`: option names/defaults, the Piper voice model catalog, and the default Sendspin service URL.
- `services/clock_callouts.py`: `ClockCallouts` — race-clock callout phrase planning (`plan()`/`phrase()`), used directly by the plugin to decide tone-vs-voice for `Evt.RACE_CLOCK_CALLOUT` and by the service's `SpeechEngine` for pre-cache generation.
- `locales.json`: localized phrase text (lap word, clock-callout thresholds, schedule countdown phrases) read independently by both the plugin (for immediate race-clock text) and the service's `race/speech.py` (for scheduled-countdown and pre-cache text).
- `sendspin_player/`: Vite/React/shadcn source for the browser player; production output is written into `custom_plugins/race_voice/player/` by `npm run build:plugin`.

The plugin performs no local synthesis, queueing, or audio upload. `const.py`, `services/clock_callouts.py` and `locales.json` are shared source files: the service package imports them directly (`from custom_plugins.race_voice... import ...`), which is why the Docker image and `.deb` build also bundle `custom_plugins` alongside `sendspin_service`. Piper synthesis (`piper.py`) and lap-segment planning (`lap_callouts.py`) used to live here too but are service-only code with no plugin caller, so they now live under `sendspin_service/` (see below) rather than being shared from the plugin's own directory.

## Service Package

`sendspin_service/` is split into three domain subpackages, with the process entry point (`server.py`, `__init__.py`, `__main__.py`) at the top level:

- `server.py`: `SendspinService`/`ServiceConfig` — process entry point, env/argument parsing, the `aiohttp.web` app, the `/health` endpoint, the optional bearer-token middleware for `/v2/*`, and wiring of the race-event routes to one `SendSpinServer` instance and one `local` playback destination.

`race/` — race-event admission and planning:

- `race_ingest.py`: `RaceIngest` — owns session/ownership fencing, full-state snapshot validation and admission, the clock exchange, and per-event admission (`ContextGate`); wires one shared `PreparationPlanner` to every configured named `Destination`, and exposes `add_routes()` for the HTTP surface below.
- `race_planner.py`: `PreparationPlanner` (bounded, race-aware synthesis admission shared across all destinations), `PlaybackPlanner` (per-destination bounded playback queue and cancellation), `Destination` (a `PlaybackSink` plus an optional `accepts` filter predicate), `SendspinPlaybackSink` (adapts one `SendSpinServer` to the planner), and `CalloutPlan`/`priority_for()`.
- `race_protocol.py`: `RaceEvent`, `Context`, `ClockMapping`, `Admission`, `ContextGate` — strict parsing/validation for the `race-events/1` wire format and the single-owner admission/clock state machine.
- `race_relay.py`: `RaceRelaySink` — a `PlaybackSink` forwarding a `CalloutPlan`'s audio (by sha256 content reference, with cache-miss upload recovery) and context to a remote relay over the `race-relay/1` wire contract. `deadline`/`target` cross the hop as approximate wall-clock timestamps rather than through a real `ClockMapping` exchange.
- `race_relay_receiver.py`: `RaceRelayReceiver` — the other end of `race-relay/1`: accepts relayed audio+context and plays it through this host's own local output, bypassing `PreparationPlanner`/`SynthesisWorker` entirely since the audio already arrived synthesized. Opt-in (`--relay-receiver-enabled`), gated by its own token separate from `--api-token`/`--relay-token`.
- `race_schedule.py`: `RaceSchedule` — asyncio timer-based scheduled-race countdown callbacks (60/30/10/5 s), keyed by session/generation/target so a repeated plan does not replay handled thresholds. Owns the threshold constants outright now (`services/schedule.py`'s old thread-based `ScheduleCalloutManager` was dead code, removed).
- `speech.py`: `SpeechEngine` — maps a `RaceEvent` to one or more `CalloutSegment`s using `lap_callouts.py` and the plugin's shared `services/clock_callouts.py`, drives per-segment synthesis through the worker, and implements the explicit `prepare()` pre-cache operation.
- `lap_callouts.py`: `LapCalloutSegments` — reusable pilot/lap-number/lap-time segment planning; service-only, no plugin caller.
- `cache_commands.py`: `CacheCommands` — runs one manual `prepare`/`clear_cache` job at a time outside the HTTP handler, with bounded command-result history and coalesced temporary-lap cleanup on heat/competition change.
- `telemetry.py`: `record()` — a single dependency-free function that logs one JSON line per pipeline stage per event, gated on the logger's effective level. Its logger name is hardcoded to `sendspin_service.telemetry` (not path-derived) since it's a documented, stable operator attach point (see [latency-validation.md](latency-validation.md)).

`synthesis/` — Piper/ONNX synthesis:

- `piper.py`: `PiperSynthesizer` — Piper model download/loading, ONNX Runtime session setup, synthesis, text normalization, WAV cache writes, and an abstract `_run_native()` hook that subclasses implement. Has no dependency on `gevent`, since the worker subprocess (the only production consumer) never installs it.
- `gevent_piper.py`: `GeventPiperSynthesizer` — a `PiperSynthesizer` subclass implementing `_run_native()` via gevent's hub-threadpool, for a gevent-patched caller.
- `synthesis.py`: `SynthesisWorker` — bounded asyncio supervision of the persistent synthesis subprocess: a priority queue, request deduplication/promotion for identical in-flight jobs, and subprocess lifecycle management.
- `synthesis_worker.py`: the private, line-delimited worker protocol run inside the isolated child process. `WorkerSynthesizer` subclasses `PiperSynthesizer` to run natively (no gevent, since the child is a plain, unpatched interpreter) and to key its WAV cache on a content hash of text, tuning, the installed `piper-tts` version, and the model file contents.

`playback/` — Sendspin/audio transport:

- `sendspin.py`: `SendSpinServer` — a synchronous adapter around `aiosendspin` that owns a background asyncio loop and exposes blocking `play()`/`stop()`.
- `audio_queue.py`: the shared `Priority` `IntEnum` and `WavItem` dataclass used by the planners and the Sendspin adapter.
- `audio_cache.py`: `AudioCache` — indexes the bundled asset WAVs by content hash for `/health`'s `bundled_audio` field; its upload-reuse (`resolve()`) path exists for content-addressed producers but is not wired into the current `/v2/*` routes, which never upload raw audio.
- `player.py`: `add_player_routes()` — optional static routes serving a built browser player directory at `/`, used by the Docker image (`SENDSPIN_PLAYER_DIR`); unset (and unused) for the `.deb` install, which instead serves the player through the RH plugin's own `/player` blueprint.

Cross-subpackage imports are absolute (`sendspin_service.playback.audio_queue`, etc.), not `..`-relative — this repo's ruff config (`TID252`) forbids parent-relative imports. Same-subpackage imports stay relative.

The same service code is packaged in two deployment formats:

- `.deb` + systemd (`packaging/deb/`, built via `packaging/nfpm.yaml`) for local Pi/Debian timing-server installs. The browser player is served by the RH plugin at `/player`.
- Docker image (`Dockerfile`) for container/cloud deployments, with the browser player build copied in and served from `/`.

Service endpoints, registered unconditionally by `RaceIngest.add_routes()`:

- `GET /health`: service status, package `version`, `aiosendspin` version, configured hosts/ports, connected client/player count, the configured body-size limit, whether an API token is required, and bundled-asset hashes.
- `GET/POST /v2/session`, `PUT /v2/state`, `POST /v2/clock`, `POST /v2/events`, `POST /v2/commands`, `GET /v2/commands/{command_id}`: race-event ingest, described in [service-audio-planner.md](service-audio-planner.md) and [race-event-contract.md](race-event-contract.md).

## Plugin and Service Compatibility

The plugin only speaks the `/v2/*` race-event API; the service registers those routes unconditionally. Per the project's hard-cutover policy (see `AGENTS.md`), there is no v1 compatibility mode: an old, pre-cutover service without the `/v2/*` routes is not supported, and the plugin surfaces the resulting connection failure rather than falling back to a legacy path. Package release numbers do not need to match; the service `version` in `/health` is diagnostic metadata, not an API version.

## Playback Behavior

`SendSpinServer` runs an asyncio event loop in a dedicated thread and exposes blocking `play()` / `stop()` methods, called by each destination's `SendspinPlaybackSink` off the planner's asyncio loop (`asyncio.to_thread`).

Important behavior:

- Consecutive play calls append to the active stream instead of restarting it.
- Scheduled static sounds carry a `play_at` timestamp in the publisher's monotonic clock. The plugin derives every stage-tone target from `pi_staging_at_s` + its index and the buzzer's from `pi_starts_at_s`, all on `Evt.RACE_STAGE`, sends them on `/v2/events` requests, and the service maps each through its clock exchange (`ClockMapping`) into the `CalloutPlan.target` used for Sendspin scheduling.
- Scheduled race sounds target RotorHazard's server-side tone time. Built-in RotorHazard browser tones may not line up exactly because they are driven by browser timer and audio scheduling.
- Late-joining browser clients are added to the active stream group.
- The stream is stopped after the queued audio has finished.

## Audio Queue and Priority

`PreparationPlanner` (shared across all destinations) and each destination's `PlaybackPlanner` both order work using the same `Priority` `IntEnum` from `sendspin_service/playback/audio_queue.py`, computed per event by `race_planner.priority_for()`:

| Priority | Value | Used for |
|----------|-------|----------|
| SIGNAL   | -1    | Race-clock callouts, scheduled-race countdown speech, staging tones, and the race-start buzzer (every `tone`/`countdown` event except the audio-check tone) |
| HIGH     | 0     | Winner announcements, the manual test phrase (sent with `winner_flag: true`), and the audio-check tone |
| NORMAL   | 1     | General voice announcements (non-winner) |
| LOW      | 2     | Crossing beeps (earmarked, not yet emitted by the current plugin) |
| LAP      | 3     | Lap callouts; always yields to every other class |

Expired jobs are dropped before playback starts, both at preparation admission and again at the sink. `PreparationPlanner` keeps at most four pending laps (replacing the same pilot or the oldest pending lap) plus 32 other pending announcements; each `PlaybackPlanner` bounds its own queue independently (32 pending items, 8 MiB of retained audio, with some byte budget reserved for a SIGNAL-priority interruption). A SIGNAL event cancels active lower-priority playback and drops pending laps; only laps are discarded automatically when a signal arrives.

## Synthesis Concurrency

Speech synthesis never runs inside RotorHazard's process. Each admitted, non-tone `CalloutPlan` is turned into one or more `CalloutSegment`s by `SpeechEngine` and sent to `SynthesisWorker.request()`, which queues the request by priority, shares an in-flight result across identical concurrent requests (and promotes a queued background job if a higher-priority identical request arrives), and exchanges bounded JSON lines with a single persistent child process running `sendspin_service/synthesis/synthesis_worker.py`.

That child process is a fresh, unpatched Python interpreter — it never inherits RotorHazard's `gevent.monkey.patch_all()` state. Inside it, `WorkerSynthesizer` (a `PiperSynthesizer` subclass) runs Piper/ONNX inference directly, rather than through `GeventPiperSynthesizer`'s gevent-hub-threadpool isolation, because that isolation exists to protect a gevent event loop that isn't present in the child. ONNX Runtime is still configured with at most two intra-op compute threads (one on a single- or dual-core host). Requests to the child are processed one at a time, in priority order; the worker restarts on failure or timeout, and the next request starts a fresh child.

`GeventPiperSynthesizer`'s gevent-native-thread isolation logic is exercised directly by `tests/helpers/gevent_synthesis_probe.py` under RH-style monkey-patching, but that code path is not reachable from the current production runtime, since nothing constructs `GeventPiperSynthesizer` inside a gevent-patched process anymore.

## Cache Layout

```text
race-voice-cache/            (SENDSPIN_RACE_CACHE_DIR / --race-cache-dir; default under the service's own state directory, not RotorHazard's data directory)
  models/
    {model_name}.onnx
    {model_name}.onnx.json
  tts/
    {model_name}/
      precache/
        pilots/
        laps/
        clock/
        schedule/
      tmp/
      test/
      {sha256}.wav
```

Cache filenames are content hashes computed by `WorkerSynthesizer.cache_key()`: a SHA-256 digest over the normalized phrase text, the speed/noise/noise_w tuning, the installed `piper-tts` package version, and the selected model's `.onnx`/`.onnx.json` file contents (hashed once per file-signature change, not per phrase). Changing the model files, the Piper library version, the text, or any tuning value produces a different hash, so stale audio is never reused across those changes; the `{model_name}/` directory split is an additional, redundant safeguard.

## Lap Callout Segments

Lap callouts are synthesized as reusable segments and played as one job:

1. **Pilot segment**: `"{callsign},"`, stored in `precache/pilots/`.
2. **Lap-number segment**: `"{Lap} {n}"`, stored in `precache/laps/`.
3. **Lap-time segment**: the dynamic phonetic lap time, synthesized into `tmp/`.

This avoids pre-generating every pilot/lap combination while still keeping common parts cached before racing starts.

## Pre-Cache Rebuilds

The **Prepare pre-cache** button sends a `prepare` command to the service over `/v2/commands`. `CacheCommands` admits one manual job at a time and runs `SpeechEngine.prepare()`, which fills missing race-clock callout phrases (`services/clock_callouts.py`), scheduled-race countdown phrases (from the same locale data as live countdowns, sharing `precache/clock`), lap-number segments and pilot-name segments (`lap_callouts.py`) for the current roster, reusing any already-valid files and yielding between phrases so live synthesis is never starved. Progress (`completed`/`total`/`generated`/`reused`) is reported back to the plugin through `GET /v2/commands/{command_id}`.

Operators should run **Prepare pre-cache** after first setup or voice model/settings changes when they want predictable phrases prepared before racing.
