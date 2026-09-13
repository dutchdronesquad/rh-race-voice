# Usage Guide

Race Voice generates RotorHazard callout WAV files on the timing server and sends them to `sendspin-service` over HTTP. The RotorHazard plugin serves its browser player at `/player`; the Docker image also includes a player at `/`.

**For use with the RotorHazard plugin, we recommend the [`.deb` installation](#sendspin-service) on the same Raspberry Pi OS machine as RotorHazard.** [Docker Compose](#docker-image) is an optional extra for hosting the service in the cloud.

For the standard Raspberry Pi setup, install both the plugin ZIP and service `.deb` from the same release. The plugin provides RotorHazard integration and the `/player` page. The `.deb` runs Sendspin independently as a systemd service on that same Pi, with its own Python runtime and dependencies.

## Setup

1. [Install and verify `sendspin-service`](#sendspin-service).
2. In RotorHazard, open **Settings** -> **Race Voice**.
3. Enable **Plugin audio**.
4. Set **Sendspin service URL** to `http://127.0.0.1:8766` for a service on the RotorHazard host, or `http://<service-host>:8766` for a [separate machine](#running-on-a-separate-machine).
5. Choose a voice model and speech settings.
6. Open the browser player from the RotorHazard UI on the playback device, for example `<RotorHazard UI base URL>/player`.
7. Set normal RotorHazard browser Voice Volume and Tone Volume to `0` on clients that should not play duplicate built-in callouts, staging tones, or start sounds.
8. Use **Generate test phrase** or **Play audio check**.

## Sendspin Service

### Install on Debian or Raspberry Pi OS

**Already running Sendspin in Docker on this machine?** [Stop that container first](#port-conflicts). Both installations use ports `8766` and `8927` by default, so the `.deb` service cannot start while the container occupies those ports.

The `.deb` package is designed to run Sendspin as a standalone service on **the same Raspberry Pi running RotorHazard**, using 64-bit Raspberry Pi OS. It also supports 64-bit Debian systems with systemd (`arm64` and `amd64`). Open a terminal on the RotorHazard machine (or connect over SSH) and paste:

```shell
curl -fL https://github.com/dutchdronesquad/rh-race-voice/releases/latest/download/install-sendspin-service.sh -o install-sendspin-service.sh &&
  bash install-sendspin-service.sh
```

Enter your sudo password if prompted. The installer detects `arm64` or `amd64`, downloads the latest release package, verifies its checksum, installs it, and ensures the service runs and starts at boot. It includes its own Python runtime and dependencies. Wait for **installed and running** before continuing.

Use `race_voice.zip` from the same latest release. In RotorHazard, enable **Plugin audio** and leave **Sendspin service URL** at `http://127.0.0.1:8766`. Open the `/player` page on your playback device, press **Connect**, then use **Play audio check** in RotorHazard.

If `curl` is missing, run `sudo apt update && sudo apt install -y curl`, then repeat the command. A 32-bit OS (`armhf`) is not supported by the release packages.

For an older plugin release, pass its exact release tag to the downloaded installer: `bash install-sendspin-service.sh v<version>` (replace `v<version>` with the tag shown on its GitHub release).

<details>
<summary>Manual package installation</summary>

Run `dpkg --print-architecture` on the service machine. Download the matching `sendspin-service_<version>_arm64.deb` or `sendspin-service_<version>_amd64.deb` from the [same release as your plugin](https://github.com/dutchdronesquad/rh-race-voice/releases). In the directory containing the downloaded package, run `sudo apt install ./<filename>`, replacing `<filename>` with its actual name. The package enables and starts the service automatically.

</details>

### Verify the service and connect a player

On the service machine, check that the service is running and its HTTP API responds:

```shell
systemctl status sendspin-service --no-pager
curl --fail http://127.0.0.1:8766/health
```

The status should show `active (running)`, and `/health` should return JSON including the installed service `version`. Keep the plugin ZIP, `.deb` package, and Docker image on the same Race Voice release version; Race Voice warns in the RotorHazard UI when the plugin and service versions differ.

In RotorHazard, enable **Plugin audio** under **Settings** -> **Race Voice** and set **Sendspin service URL** to `http://127.0.0.1:8766` when both run on the same host. On the playback device, open `<RotorHazard UI base URL>/player`, set the player's **Server URL** to `http://<service-host>:8927`, and press **Connect**. Replace `<service-host>` with the service machine's LAN IP address or hostname reachable from that device. Use **Play audio check** in RotorHazard to confirm sound.

Port `8766` is the HTTP API used by RotorHazard; port `8927` is the Sendspin endpoint used by players. Allow playback devices to reach TCP port `8927` if the service machine has a firewall. On a separate playback device, `127.0.0.1` refers to that device, so use the service machine's LAN address in the player.

If the service is not running or the health check fails, inspect its logs:

```shell
journalctl -u sendspin-service -n 80 --no-pager
```

### Running on a separate machine

This is an advanced option for a separate Debian-based machine on your local network. The standard `.deb` setup uses the same Raspberry Pi as RotorHazard. For cloud hosting, follow the [Docker Compose instructions](#docker-image).

Install the package on the service machine using the steps above. By default, the HTTP API listens only on `127.0.0.1`, so RotorHazard on another machine cannot reach it.

1. On the service machine, open the configuration:

   ```shell
   sudo nano /etc/default/sendspin-service
   ```

   Change `SENDSPIN_INGEST_HOST=127.0.0.1` to `SENDSPIN_INGEST_HOST=0.0.0.0` to listen on its network interfaces, then save the file.

2. Apply the change:

   ```shell
   sudo systemctl restart sendspin-service
   ```

3. From the **RotorHazard host**, check the connection, replacing `<service-host>` with the service machine's LAN IP address or hostname:

   ```shell
   curl --fail http://<service-host>:8766/health
   ```

4. Set RotorHazard's **Sendspin service URL** to `http://<service-host>:8766`. In each browser player, set **Server URL** to `http://<service-host>:8927`, connect, and run **Play audio check**.

If a firewall is enabled, allow TCP port `8766` from the RotorHazard host and TCP port `8927` from playback devices. The default API has no authentication; keep it on a trusted LAN and restrict access to port `8766` to the RotorHazard host.

### Configuration and upgrades

Default config is stored in `/etc/default/sendspin-service`:

```shell
SENDSPIN_INGEST_HOST=127.0.0.1
SENDSPIN_INGEST_PORT=8766
SENDSPIN_HOST=0.0.0.0
SENDSPIN_PORT=8927
SENDSPIN_ADVERTISE=true
SENDSPIN_MAX_BODY_MB=50
```

Restart the service with `sudo systemctl restart sendspin-service` after editing the configuration. To upgrade to the latest release, run the installer again and update the plugin to the same release. For a specific release, pass its tag to the installer as shown above. The installer preserves `/etc/default/sendspin-service` and restarts the service; verify `/health` and playback again afterwards.

The service API accepts inline WAV payloads via `wav_files`. It does not accept filesystem paths. This keeps the packaged service independent of RotorHazard/plugin directory permissions while running with `DynamicUser=yes`.

## Docker Image

Docker Compose is an **optional extra for cloud hosting**. For use with the RotorHazard plugin, the recommended installation is the [`.deb` package](#sendspin-service) on the same Raspberry Pi OS machine as RotorHazard. The container runs independently of the RotorHazard machine and includes a browser player at `/`. RotorHazard and playback clients must be able to reach that cloud service.

**Already running the `.deb` service on this machine?** [Stop and disable it first](#port-conflicts). Docker publishes the same host ports (`8766` and `8927`) by default and cannot start while the service occupies them. Use one deployment per machine for the normal setup.

For a quick local test of the cloud image:

```shell
docker run --rm \
  -p 8766:8766 \
  -p 8927:8927 \
  ghcr.io/dutchdronesquad/sendspin-service:latest
```

For the cloud deployment, run Docker Compose on your cloud server from a checkout of this repository:

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

### Port conflicts

The `.deb` service and Docker variant both use TCP ports `8766` (HTTP API) and `8927` (Sendspin) by default. If you start both on the same machine, the second deployment can fail with `Address already in use` or `port is already allocated`.

Choose the deployment you want to keep:

- **Keep the `.deb` service on your Raspberry Pi:** run `docker compose down` from the directory of the Sendspin Compose project, then run `sudo systemctl restart sendspin-service`. For a container started with `docker run`, find it with `docker ps` and remove that Sendspin container with `docker rm -f <container-name>` (replace the placeholder with its actual name).
- **Keep Docker for your cloud deployment:** run `sudo systemctl disable --now sendspin-service`, then run `docker compose up -d` from the Sendspin Compose directory. Disabling the systemd service also prevents it from starting at the next boot.

Check `systemctl status sendspin-service --no-pager` and `docker ps` to confirm which deployment is running. If a port is still occupied, `sudo ss -ltnp '( sport = :8766 or sport = :8927 )'` shows the listeners. Verify the retained service with **Play audio check**.

Running both deliberately requires separate host ports for each deployment and matching URLs in RotorHazard and every player. Otherwise, audio may be sent to one service while players connect to the other.

### Other issues

- **No audio in `/player`**: confirm `sendspin-service` is running, the player Server URL points at the same service RotorHazard sends to, and the player is connected.
- **Service unreachable**: confirm `curl http://127.0.0.1:8766/health` works from the RotorHazard host.
- **Outdated service**: compare the plugin release with `version` from `curl http://127.0.0.1:8766/health`. If they differ, reinstall or upgrade the component that does not match the intended Race Voice release.
- **Player page unreachable**: confirm `<RotorHazard UI base URL>/player` works from the playback device.
- **Some players hear different or duplicate audio**: confirm only one Sendspin service is active for the event, or verify each service uses unique ports and every player is configured for the intended Server URL. Check both `systemctl status sendspin-service` and `docker ps` on hosts where you have tested container deployments.
- **Duplicate voice callouts or tones**: set RotorHazard Voice Volume and Tone Volume to `0` in regular RotorHazard browser clients.
- **First phrase is slow**: the selected Piper model may still be downloading or loading.
- **Browser playback stutters**: test Safari or Chrome incognito with extensions disabled, then validate on the race network.
