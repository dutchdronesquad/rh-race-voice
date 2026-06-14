# Usage Guide

Race Voice generates RotorHazard callout WAV files on the timing server and sends them to `sendspin-service` over HTTP. The RotorHazard plugin serves its browser player at `/player`; the standalone service/container serves its player at `/`.

The plugin ZIP and service `.deb` are separate release assets. Install both for a normal RotorHazard setup: the plugin provides RotorHazard integration and the `/player` page, while `sendspin-service` provides playback transport.

## Setup

1. Install and start `sendspin-service`.
2. In RotorHazard, open **Settings** -> **Race Voice**.
3. Enable **Plugin audio**.
4. Confirm **Sendspin service URL** is `http://127.0.0.1:8766`.
5. Choose a voice model and speech settings.
6. Open the browser player from the RotorHazard UI on the playback device, for example `<RotorHazard UI base URL>/player`.
7. Set normal RotorHazard browser Voice Volume and Tone Volume to `0` on clients that should not play duplicate built-in callouts, staging tones, or start sounds.
8. Use **Generate test phrase** or **Play audio check**.

## Sendspin Service

Download the `.deb` for your architecture from the [GitHub Releases page](https://github.com/dutchdronesquad/rh-race-voice/releases). Use `arm64` for 64-bit Raspberry Pi OS (the default since Raspberry Pi OS Bookworm) and `amd64` for x86 machines.

```shell
wget https://github.com/dutchdronesquad/rh-race-voice/releases/download/v<version>/sendspin-service_<version>_arm64.deb
sudo apt install ./sendspin-service_<version>_arm64.deb
```

Common checks:

```shell
systemctl status sendspin-service
curl http://127.0.0.1:8766/health
journalctl -u sendspin-service -n 80 --no-pager
```

Default config is stored in `/etc/default/sendspin-service`:

```shell
SENDSPIN_INGEST_HOST=127.0.0.1
SENDSPIN_INGEST_PORT=8766
SENDSPIN_HOST=0.0.0.0
SENDSPIN_PORT=8927
SENDSPIN_ADVERTISE=true
SENDSPIN_MAX_BODY_MB=50
```

The service API accepts inline WAV payloads via `wav_files`. It does not accept filesystem paths. This keeps the packaged service independent of RotorHazard/plugin directory permissions while running with `DynamicUser=yes`.

## Docker Image

The Docker image is the container deployment path for `sendspin-service`. For a normal Raspberry Pi timing-server install, use the `.deb` package instead.

Basic local container run:

```shell
docker run --rm \
  -p 8766:8766 \
  -p 8927:8927 \
  ghcr.io/dutchdronesquad/sendspin-service:latest
```

Docker Compose:

```shell
cp .env.example .env
sed -i "s/change-this-token/$(openssl rand -hex 32)/" .env
docker compose up -d
```

The included Compose file builds the local Dockerfile by default. To run the published image instead, replace the `build:` block with `image: ghcr.io/dutchdronesquad/sendspin-service:latest`.
Container runtime settings are read from `.env`; the checked-in `.env.example` contains the default host, port, advertise, body-size, player-dir, and API-token settings.

The container serves the browser player at `http://<container-host>:8766/`. The HTTP ingest API is on the same port under `/v1`, and the health check is available at `/health`. Browser clients connect to the Sendspin WebSocket endpoint on port `8927` at `/sendspin`.

Container defaults:

```shell
SENDSPIN_INGEST_HOST=0.0.0.0
SENDSPIN_INGEST_PORT=8766
SENDSPIN_HOST=0.0.0.0
SENDSPIN_PORT=8927
SENDSPIN_ADVERTISE=false
SENDSPIN_MAX_BODY_MB=50
SENDSPIN_PLAYER_DIR=/opt/sendspin-service/player
```

For a public container deployment, set `SENDSPIN_API_TOKEN` before exposing port `8766`. Producers must send `Authorization: Bearer <token>` for `/v1/play` and `/v1/stop`. Keep it unset only for local-only testing on a trusted machine.

The image does not include the RotorHazard plugin. The bundled player is for direct container use; the normal RotorHazard plugin ZIP still serves its own `/player` route.

Manual playback test:

```shell
WAV=$(base64 -w0 custom_plugins/race_voice/assets/moavii-foreign.wav)
curl -s -X POST http://127.0.0.1:8766/v1/play \
  -H "Content-Type: application/json" \
  -d "{\"wav_files\":[{\"name\":\"test.wav\",\"data\":\"$WAV\"}],\"priority\":\"high\",\"volume\":1.0}"
```

## Package Build

Maintainer build requirements: `uv`, `nfpm`, and a local Python 3.11+ interpreter for the build script.

```shell
python -m tools.build_sendspin_service_deb
```

Build for a specific architecture on a matching runner:

```shell
python -m tools.build_sendspin_service_deb --architecture arm64
```

For local install testing, copy the `.deb` to `/tmp` first so `apt` can read it through its `_apt` sandbox user:

```shell
rm -f /tmp/sendspin-service_*.deb
cp dist/sendspin-service_*_amd64.deb /tmp/
sudo apt install /tmp/sendspin-service_*_amd64.deb
```

Reinstall the same local version:

```shell
sudo apt install --reinstall /tmp/sendspin-service_*_amd64.deb
```

Package CI:

- Pull requests that touch service/package files build the `amd64` `.deb` through `.github/workflows/build.yaml`.
- Published GitHub Releases build and upload both `amd64` and `arm64` `.deb` assets through `.github/workflows/release.yaml`.
- The shared build logic lives in `.github/actions/build-sendspin-deb/action.yaml`.

## Callouts

Race Voice hooks into two RotorHazard filter events and generates the following callouts automatically while plugin audio is enabled:

| Event | Callout | Priority |
|---|---|---|
| Pilot completes a lap | `"{callsign}, Lap {n}, {m:ss.f}"` | Normal |
| Race winner announced | `"Winner is {callsign}!"` (or localized equivalent) | High |
| Race clock callout | `"1 minute"` / `"30 seconds"` / `"10 seconds"`; final `5` to `1` uses `stage.wav`, `0` uses `buzzer.wav` | High |
| Scheduled race countdown | `"Race begin in 60 seconds"` / `"30"` / `"10"` / `"5"` | High |
| Race staging tone | Bundled `stage.wav` | High |
| Race start | Bundled `buzzer.wav` | High |

Lap 0 (first crossing without a completed lap) does not produce a callout. Winner callouts are generated from the RotorHazard phonetic text filter, which fires when RotorHazard determines the race winner.

Countdown callouts are generated when a race is scheduled via the RotorHazard schedule panel. They are cancelled automatically if the schedule is replaced or cancelled before the race starts.

The callsign used in a lap callout is the pilot's **phonetic name** if set, otherwise the **callsign**. Set phonetic names in the RotorHazard pilot list for better pronunciation with the selected voice model.

## Browser Player

The Sendspin browser player connects to `sendspin-service` and plays synchronized audio. Open it at `<RotorHazard UI base URL>/player` on the playback device, or at `http://<container-host>:8766/` when using the Docker image.

Multiple devices can connect simultaneously and will receive the same audio in sync.

### Connecting

1. Open the player URL on the playback device.
2. Confirm the **Server URL** in the player points to the `sendspin-service` HTTP endpoint (default `http://<timingserver>:8927`).
3. Press **Connect**. The status badge shows **Ready** when the player is connected.

The player stores the server URL in the browser's local storage and reconnects automatically if the connection drops.

### Sync modes

| Mode | Description | Best for |
|---|---|---|
| **Sync** | Sample-level correction via small buffer resets | Local wired or fast Wi-Fi networks |
| **Quality** | Gradual playback-rate adjustment | Tolerates network jitter; avoids audible resets |
| **Quality local** | Uses device clock as reference | Offline or unreliable connections |

Use **Sync** for most race-day setups. Switch to **Quality** if playback resets are audible on the local network.

### Controls

- **Volume / Mute**: adjusts local playback volume and mutes the output. Settings are stored per device.
- **Share**: shows a QR code and URL for the player page. Useful for distributing the player link to spectators at the event.
- **Diagnostics**: expands a panel with stream format, time-sync state, sync error, output latency, correction method, and playback rate. Useful for debugging sync issues.

### WindowsSpin

[WindowsSpin](https://github.com/sendspin/windowsspin) is a native Windows application that connects to the same Sendspin stream. Configure it with the `sendspin-service` host and port (`8927` by default). It will receive the same synchronized audio as the browser player.

## Settings

### Options

- **Enable plugin audio**: Turns Race Voice callout generation on or off.
- **Sendspin service URL**: HTTP endpoint for `sendspin-service`. Default: `http://127.0.0.1:8766` when the plugin and service run on the same host.
- **Sendspin service timeout**: HTTP timeout for queue/stop requests to `sendspin-service`.
- **Voice model**: Piper voice model. Models are downloaded once and reused.
- **Speech speed**: Speaking rate. `1.0` is Piper default. Range: `0.5`–`2.0`.
- **Noise scale**: Voice variation. `0.0` is monotone, `1.0` is expressive. Default: `0.667`.
- **Phoneme width noise**: Duration variation between phonemes. `0.0` is uniform, `1.0` is varied. Default: `0.8`.
- **Test phrase**: Phrase generated by the **Generate test phrase** button.

### Quick buttons

- **Generate test phrase**: Synthesizes the test phrase with the current voice settings and sends it to the Sendspin service. Use this to verify end-to-end audio before race day.
- **Play audio check**: Plays a bundled demo WAV without synthesizing TTS. Confirms `sendspin-service` is reachable and clients receive audio even if no voice model is loaded yet.
- **Stop audio**: Immediately stops all queued and active audio on the Sendspin service. Useful when a callout needs to be cut mid-playback.
- **Clear TTS cache**: Removes all generated WAV files. Use after a voice model change to avoid stale audio from the previous model.
- **Rebuild pre-cache**: Pre-generates WAV files for race-clock callouts, the current heat's pilot names, lap segments, and schedule phrases. Run this after startup or after changing voice settings so common phrases are ready before racing starts.

## Cache Layout

Generated files live under the RotorHazard data directory:

```text
race_voice_cache/
  models/                 downloaded Piper ONNX models
  tts/<model>/            cached phrases
  tts/<model>/precache/pilots/
                           pre-generated pilot-name segments
  tts/<model>/precache/laps/
                           pre-generated "Lap [n]" segments
  tts/<model>/precache/clock/
                           race-clock callout phrases
  tts/<model>/precache/schedule/
                           scheduled-race countdown phrases
  tts/<model>/tmp/        ephemeral lap-time phrases
  tts/<model>/test/       generated test phrases
```

Cache behavior:

- `tmp/` is cleared whenever a heat is selected.
- `precache/` keeps existing reusable phrases. Use **Rebuild pre-cache** to generate race-clock callout phrases, schedule phrases, current-heat pilot-name segments, and lap-number segments on demand.
- `tmp/` and `precache/` are cleared on RotorHazard data reset.
- **Clear TTS cache** removes all WAV files for the selected model.

## Operational Notes

- Race Voice does not disable RotorHazard's built-in browser speech or tone playback. Set Voice Volume and Tone Volume to `0` on regular RotorHazard browser clients to avoid duplicate callouts, staging tones, and start sounds.
- The first use of a voice model requires internet access to download model files. Racing can run offline after the selected model has been cached.
- Callouts are generated server-side; browser-specific RotorHazard voice settings do not affect Race Voice output.
- Staging tones and the race-start buzzer are static WAV files played through Sendspin. They require a RotorHazard build that provides `Evt.RACE_STAGE_TONE`.
- Race-clock callouts require a RotorHazard build that provides `Evt.RACE_CLOCK_CALLOUT`. During the final five seconds of a running countdown heat, Race Voice uses static stage tones and a buzzer instead of spoken TTS.
- Scheduled race sounds are sent to `sendspin-service` with a relative playback delay, so the service can run on the RotorHazard host or another reachable machine without sharing a monotonic clock.
- Race Voice schedules race sounds against RotorHazard's server-side tone time. If RotorHazard browser Tone Volume is still enabled during comparison, its browser-generated tones may sound slightly later because they depend on browser timer and audio scheduling.
- If no Sendspin browser player is connected, generated audio is dropped and logged.

## Troubleshooting

- **No audio in `/player`**: confirm `sendspin-service` is running, the player Server URL points at the same service RotorHazard sends to, and the player is connected.
- **Service unreachable**: confirm `curl http://127.0.0.1:8766/health` works from the RotorHazard host.
- **Player page unreachable**: confirm `<RotorHazard UI base URL>/player` works from the playback device.
- **Duplicate voice callouts or tones**: set RotorHazard Voice Volume and Tone Volume to `0` in regular RotorHazard browser clients.
- **First phrase is slow**: the selected Piper model may still be downloading or loading.
- **Browser playback stutters**: test Safari or Chrome incognito with extensions disabled, then validate on the race network.
