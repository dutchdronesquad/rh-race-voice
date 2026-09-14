# Architecture

## Runtime Flow

```text
RotorHazard event/filter
  -> RaceVoicePlugin
  -> PiperSynthesizer
  -> WAV cache
  -> AudioQueue
  -> SendspinServiceClient (HTTP)
  -> sendspin-service
  -> Sendspin browser/player clients
```

The main RotorHazard sources are `Flt.EMIT_PHONETIC_DATA`, `Flt.EMIT_PHONETIC_TEXT`, `Evt.RACE_CLOCK_CALLOUT`, `Evt.RACE_STAGE_TONE`, `Evt.RACE_START`, `Evt.HEAT_SET`, and scheduled race events.

The RotorHazard plugin owns event handling, TTS generation, caching, enqueueing, and the browser player route at `/player`. `sendspin-service` owns `aiosendspin`, player connections, stream state, and the Sendspin player endpoint on port `8927`.
Staging tones from `Evt.RACE_STAGE_TONE` and the race-start buzzer from `Evt.RACE_START` are queued as static WAV files through the same Sendspin service path.

## Plugin Package

- `piper.py`: Piper model download, model loading, synthesis, and WAV cache writes; reused by the service's isolated synthesis worker, not run in-process by RotorHazard.
- `event_adapter.py`: RotorHazard event/filter integration and UI registration; publishes race events to `sendspin-service` instead of synthesizing audio itself.
- `event_output.py`: bounded HTTP event delivery to the service's `/v2/*` race-event routes.
- `services/lap_callouts.py`: segment planning for reusable pilot/lap/time callouts, shared with the service.
- `services/schedule.py`: scheduled-race countdown timer mapping, shared with the service.
- `services/clock_callouts.py`: race-clock callout phrase planning, shared with the service.

The plugin no longer performs local synthesis, queueing, or direct HTTP playback uploads. The [Runtime Flow](#runtime-flow) diagram above still shows the pre-v2 in-process path and needs a follow-up update.

## Service Package

- `sendspin_service/server.py`: `aiohttp.web` service for the HTTP ingest API, health endpoint, config/env parsing, and the race-event routes.
- `sendspin_service/audio_queue.py`: shared `Priority` and `WavItem` types used by the race-event planners.
- `sendspin_service/sendspin.py`: synchronous adapter around `aiosendspin`.

The same service code is packaged in two deployment formats:

- `.deb` + systemd for local Pi/Ubuntu timing-server installs.
- Docker image for container/cloud deployments, with the browser player served from `/`.

Service endpoints:

- `GET /health`: service status, package `version`, Sendspin listen port, and connected player count.
- `GET/POST /v2/session`, `PUT /v2/state`, `POST /v2/clock`, `POST /v2/events`, `POST /v2/commands`, `GET /v2/commands/{command_id}`: race-event ingest, described in [service-audio-planner.md](service-audio-planner.md) and [race-event-contract.md](race-event-contract.md).

## Plugin and Service Compatibility

The plugin only speaks the `/v2/*` race-event API; the service registers those routes unconditionally. Per the project's hard-cutover policy (see `AGENTS.md`), there is no v1 compatibility mode: an old, pre-cutover service without the `/v2/*` routes is not supported, and the plugin surfaces the resulting connection failure rather than falling back to a legacy path. Package release numbers do not need to match; the service `version` in `/health` is diagnostic metadata, not an API version.

## Playback Behavior

`SendSpinServer` runs an asyncio event loop in a dedicated thread and exposes blocking `play()` / `stop()` methods to the service queue worker.

Important behavior:

- Consecutive play calls append to the active stream instead of restarting it.
- Jobs can provide a relative playback delay for scheduled static sounds. The plugin derives that delay from `scheduled_at_monotonic` on `Evt.RACE_STAGE_TONE` and from `rhapi.race.start_time_internal` for the race-start buzzer before sending the job to `sendspin-service`.
- Scheduled race sounds target RotorHazard's server-side tone time. Built-in RotorHazard browser tones may not line up exactly because they are driven by browser timer and audio scheduling.
- Late-joining browser clients are added to the active stream group.
- The stream is stopped after the queued audio has finished.

## Audio Queue and Priority

The service's race-event planners (`sendspin_service/race_planner.py`) admit and schedule jobs with a priority and expiry deadline, using the shared `Priority` enum from `sendspin_service/audio_queue.py`.

| Priority | Used for |
|----------|----------|
| HIGH     | Winner announcements, manual test phrase, audio check, race-clock callouts, scheduled-race countdowns, staging tones, race-start buzzer |
| NORMAL   | Lap callouts |
| LOW      | Crossing beeps (earmarked, not yet used by the current plugin) |

Expired jobs are dropped before playback starts. This avoids playing stale lap callouts after a busy event burst.

## TTS Concurrency

Piper inference and ONNX session construction run through `gevent.get_hub().threadpool.apply()`. RotorHazard calls `gevent.monkey.patch_all()` before loading plugins, so the ordinary `ThreadPoolExecutor` can use greenlets on RH's event-loop thread instead of native worker threads. It remains responsible for callout orchestration, while the blocking synthesis/model-loading operations explicitly cross the native-thread boundary.

A cooperative lock serializes those native operations, including manual warmup and test phrases. ONNX uses at most two compute threads (one on a single- or dual-core host). The bounded lap queue still keeps at most four pending laps. RH API access, status notifications, queue admission and completion callbacks stay on the calling side; do not move those operations into the native pool.

This prevents a synchronous Piper call from directly occupying the RH event-loop thread. It does not remove synthesis time, CPU contention, or Sendspin playback buffering. The regression suite reproduces RH monkey-patching in a subprocess and checks heartbeat progress during synthesis, warmup and model loading, as well as cache reuse, failure recovery and callback thread affinity.

## Cache Layout

```text
race_voice_cache/
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
      {sha1}_{speed}_{noise}_{noise_w}.wav
```

Cache keys include normalized text and synthesis parameters. The model name is part of the directory path, so changing models or voice settings cannot reuse stale audio.

## Lap Callout Segments

Lap callouts are synthesized as reusable segments and played as one job:

1. **Pilot segment**: `"{callsign},"`, stored in `precache/pilots/`.
2. **Lap-number segment**: `"{Lap} {n}"`, stored in `precache/laps/`.
3. **Lap-time segment**: the dynamic phonetic lap time, synthesized into `tmp/`.

This avoids pre-generating every pilot/lap combination while still keeping common parts cached before racing starts.

## Pre-Cache Rebuilds

Race-clock callout phrase planning lives in `services/clock_callouts.py`, using the same localized phrase logic for live event playback and manual pre-cache rebuilds.

The **Prepare pre-cache** button sends a `prepare` command to the service over `/v2/commands`; the service owns stale-generation tracking, directory cleanup, race-clock phrase generation, schedule phrase generation, lap segment generation, pilot-name generation, and completion reporting back to the plugin.

Operators should run **Prepare pre-cache** after first setup or voice model/settings changes when they want predictable phrases prepared before racing.
