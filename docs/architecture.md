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

The RotorHazard plugin owns event handling, TTS generation, caching, enqueueing, and the browser player route at `/player`. `sendspin-service` owns `aiosendspin`, player connections, stream state, and the Sendspin player endpoint on port `8927`.
Staging tones from `Evt.RACE_STAGE_TONE` and the race-start buzzer from `Evt.RACE_START` are queued as static WAV files through the same Sendspin service path.

## Plugin Package

- `plugin.py`: RotorHazard event/filter integration, synthesis orchestration, queueing, and UI callbacks.
- `piper.py`: Piper model download, model loading, synthesis, and WAV cache writes.
- `audio_queue.py`: plugin-side priority queue that keeps RotorHazard callbacks fast.
- `output.py`: HTTP output client for `sendspin-service`.
- `services/lap_callouts.py`: segment planning for reusable pilot/lap/time callouts.
- `services/precache.py`: manual pre-cache rebuild orchestration.
- `services/schedule.py`: scheduled race callout timers.

The plugin sends `/v1/play` requests with inline `wav_files` payloads. The service API does not depend on reading plugin cache files from disk.

## Service Package

- `sendspin_service/server.py`: `aiohttp.web` service for the HTTP ingest API, health endpoint, config/env parsing, play, and stop endpoints.
- `sendspin_service/audio_queue.py`: service-side priority queue.
- `sendspin_service/sendspin.py`: synchronous adapter around `aiosendspin`.

The same service code is packaged in two deployment formats:

- `.deb` + systemd for local Pi/Ubuntu timing-server installs.
- Docker image for container/cloud deployments, with the browser player served from `/`.

Service endpoints:

- `GET /health`: service status, package `version`, Sendspin listen port, and connected player count.
- `POST /v1/play`
- `POST /v1/stop`

`POST /v1/play` accepts `wav_files` entries with base64 WAV data plus optional `text`, `priority`, `expiry_sec`, `play_at_delay_sec`, and `volume`.

## Playback Behavior

`SendSpinServer` runs an asyncio event loop in a dedicated thread and exposes blocking `play()` / `stop()` methods to the service queue worker.

Important behavior:

- Consecutive play calls append to the active stream instead of restarting it.
- Jobs can provide a relative playback delay for scheduled static sounds. The plugin derives that delay from `scheduled_at_monotonic` on `Evt.RACE_STAGE_TONE` and from `rhapi.race.start_time_internal` for the race-start buzzer before sending the job to `sendspin-service`.
- Scheduled race sounds target RotorHazard's server-side tone time. Built-in RotorHazard browser tones may not line up exactly because they are driven by browser timer and audio scheduling.
- Late-joining browser clients are added to the active stream group.
- The stream is stopped after the queued audio has finished.

## Audio Queue and Priority

Both the plugin and the service use a single worker queue to keep event callbacks and HTTP requests short. Jobs carry a priority and expiry deadline.

| Priority | Used for |
|----------|----------|
| HIGH     | Winner announcements, manual test phrase, audio check, scheduled-race countdowns, staging tones, race-start buzzer |
| NORMAL   | Lap callouts |
| LOW      | Crossing beeps (earmarked, not yet used by the current plugin) |

Expired jobs are dropped before playback starts. This avoids playing stale lap callouts after a busy event burst.

## TTS Concurrency

Piper synthesis uses ONNX Runtime on CPU. ONNX Runtime serializes `session.run()` calls within a process, so multiple concurrent synthesis threads do not improve throughput.

The plugin uses one shared `InferenceSession` with `intra_op_num_threads` set to the available CPU count. A `ThreadPoolExecutor` is still used to keep RotorHazard event callbacks non-blocking and to absorb bursts of race events.

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

Manual pre-cache rebuilds are handled by `services/precache.py`. The manager owns stale-generation tracking, directory cleanup, schedule phrase generation, lap segment generation, pilot-name generation, and completion notifications.

Operators should run **Rebuild pre-cache** after first setup or voice model/settings changes when they want predictable phrases prepared before racing.
