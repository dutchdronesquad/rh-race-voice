# Changelog

All notable shipped changes to Race Voice should be documented in this file.

This changelog is intentionally concise. GitHub Releases can carry the fuller change list and release assets.

## [Unreleased]

### Sendspin 9.x playback

Fixes service startup and browser connections with `aiosendspin` 9.x. The service keeps its server identity across restarts and admits browser players using the encrypted handshake. Existing unencrypted players remain supported.

### Sendspin service diagnostics

**Check Sendspin service** reports service health and checks the backend version needed by the browser player. **Play audio check** also shows these diagnostics, including when an older service cannot report its dependency version.

### Local development installation

Developers can use `bash tools/install-sendspin-service.sh --dev` from a checkout to build and install their local service changes. Repeating the command rebuilds and reinstalls the development package; `--latest` returns to a stable release.

### Independent plugin and service updates

Updating the RotorHazard plugin no longer produces a warning just because the Sendspin service has a different release number. You can keep your existing service while it supports the API required by the plugin. Service version information remains available in `/health` for troubleshooting.

## [1.0.0] - 2026-05-30

### Plugin renamed to Race Voice

The plugin has been renamed from Local Voice to Race Voice. All configuration keys, file paths, and references have been updated accordingly.

### Standalone Sendspin service

The Sendspin service now runs as a separate process, decoupled from the RotorHazard plugin. Each release ships two artifacts:

**Debian package** — the primary deployment target. Install this on the same Raspberry Pi as the race timer. It runs headless alongside RotorHazard and receives audio from the plugin over the local ingest API (port 8766).

**Docker image** — intended for running the service on a separate machine, such as a cloud relay for remote listeners. The Docker image bundles the browser player so listeners can open it directly from the container.

```
docker run -p 8766:8766 -p 8927:8927 ghcr.io/dutchdronesquad/rh-race-voice:latest
```

### Redesigned browser player

The built-in browser player has been fully rewritten in React with a dark-themed component library. The new player includes:

- Animated status ring that pulses during playback
- Audio visualizer driven by the live PCM stream
- Sync mode selector (Sync, Quality, Quality local) with per-mode descriptions
- Diagnostics panel showing format, clock sync status, sync error, output latency, correction method, and playback rate
- Scrollable activity log with colour-coded playback and warning events
- Share button that generates a QR code so other devices can join the same audio session

## [0.2.0] - 2026-05-24

### Scheduled race callouts

Race Voice now listens to RotorHazard race schedule events and can announce countdowns before a deferred race start. The default countdown phrases cover 60, 30, 10, and 5 seconds before the scheduled start, and pending countdowns are cancelled when the schedule is replaced or cancelled.

Countdown phrases are localized alongside lap callouts for the supported voice-model languages:

- English
- Dutch
- German

### Faster race-day pre-cache

Pre-cache rebuilding has been split into reusable segments for pilot names, lap numbers, and scheduled-race countdowns. This keeps repeated lap announcements fast while allowing temporary lap-time audio to remain heat-specific.

The pre-cache rebuild action now cancels stale rebuild jobs, reports completion for the current heat, and clears the relevant pre-cache folders before regenerating audio for the selected model.

### Sendspin playback

Sendspin playback can now schedule audio against a future playback time instead of only appending immediate clips. This improves scheduled countdown timing and allows future clips to be fully buffered before playback starts.

Queued audio now carries a per-job volume value, and the Sendspin backend applies linear gain to PCM audio while leaving cached WAV files unchanged.

Playback buffering has also been tightened:

- Consecutive callouts are appended to an active stream without resetting connected clients.
- Late-joining Sendspin clients are synced into the active stream.
- Stale audio is dropped before scheduling if it would start after its expiry deadline.
- The active stream is stopped once queued playback has gone idle.

## [0.1.0] - 2026-05-22

### Local voice generation

Race Voice can now generate RotorHazard callouts on the timing server with Piper TTS, without relying on browser speech or cloud services. Voice models are downloaded on first use, cached locally, and configured from the RotorHazard settings panel.

For race operators, the main benefits are:

- Callouts keep working locally after the selected voice model has been downloaded.
- Voice output is configured once in RotorHazard instead of per browser client.
- Test phrases can be generated from the settings panel before race day.

### Sendspin playback

Generated audio is queued and streamed from an in-process Sendspin source to connected playback clients. The plugin includes its own browser player at `/player`, while other Sendspin clients such as [WindowsSpin](https://github.com/sendspin/windowsspin) can also connect to the stream.

### Race-day caching

Repeated phrases are cached so they do not need to be generated again during a race. Lap callouts are split into reusable pilot/lap phrases and temporary lap-time phrases, which keeps repeated announcements fast while avoiding stale lap-time audio after a heat change.

The audio queue also tracks priorities and expiry times so stale lap callouts can be dropped instead of playing too late after a busy gate crossing.

### Operator controls

The settings panel includes quick actions for generating a test phrase, playing an audio check clip, stopping current playback, and clearing the selected voice model's TTS cache.

This release requires Python 3.12 or newer. The selected Piper model needs internet access once for the initial download, and regular RotorHazard browser clients should have Voice Volume set to `0` when Race Voice is handling callouts.
