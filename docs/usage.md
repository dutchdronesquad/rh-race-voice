# Usage Guide

Race Voice sends RotorHazard audio to one Sendspin server at a time:

- **Recommended:** the `.deb` service on the same Raspberry Pi OS machine as RotorHazard. Listen through the plugin's `/player` page or WindowsSpin on another LAN device.
- **Optional:** [Docker Compose in the cloud](#docker-image), with its own browser player. Change **Sendspin service URL** in RotorHazard to select it.

Parallel output to both servers is not supported. Keep the plugin and selected service on the same release.

## Setup

Follow the [Quick Start](../README.md#quick-start) for the complete plugin setup. Service installation, updates, and cloud setup are covered below.

## Sendspin Service

### Install

On the **RotorHazard machine**, running 64-bit Raspberry Pi OS or Debian with systemd (`arm64` or `amd64`), run:

```shell
curl -fL https://github.com/dutchdronesquad/rh-race-voice/releases/latest/download/install-sendspin-service.sh -o install-sendspin-service.sh &&
  bash install-sendspin-service.sh
```

Choose the release matching your plugin and confirm. The installer selects the package, verifies its checksum, and starts the service at installation and boot. Python and service dependencies are bundled.

If Sendspin already runs in Docker on this machine, [stop that container first](#port-conflicts).

<details>
<summary>Prerequisites and manual installation</summary>

- If `curl` is missing, run `sudo apt update && sudo apt install -y curl`.
- The release menu needs Python 3.9+. Use `--latest` or an exact release tag to skip the menu without Python.
- 32-bit systems (`armhf`) are not supported by the release packages.
- For manual installation, run `dpkg --print-architecture`, download the matching `.deb` from your plugin's [release](https://github.com/dutchdronesquad/rh-race-voice/releases), and run `sudo apt install ./<filename>` with the downloaded filename.

</details>

### Connect and test

1. In RotorHazard, enable **Settings → Race Voice → Plugin audio**. Keep **Sendspin service URL** at `http://127.0.0.1:8766`.
2. Connect a player:
   - **Browser:** open `<RotorHazard UI base URL>/player`, set **Server URL** to `http://<Pi LAN address>:8927`, and press **Connect**.
   - **WindowsSpin:** connect to the Pi's LAN address on port `8927`.
3. Click **Play audio check** in RotorHazard.

The service stays on the Pi; players can run on other LAN devices. Keep port `8766` local and allow clients to reach port `8927`. Use the Pi's LAN address in remote players, not `127.0.0.1`.

### Update or choose a version

Run the downloaded script again:

```shell
bash install-sendspin-service.sh
```

It shows the installed version and asks you to select and confirm the target release. Updates preserve `/etc/default/sendspin-service` and restart the service. The same version makes no changes; an older version is labelled **Downgrade**.

| Command | Action |
|---|---|
| `bash install-sendspin-service.sh --latest` | Install or update to the latest stable release |
| `bash install-sendspin-service.sh v1.2.3` | Select an exact release tag; replace `v1.2.3` with yours |
| `bash install-sendspin-service.sh --help` | Show all options |

Add `--yes` to confirm without prompting, including downgrades. Unattended runs also require sudo without a password prompt. Download the script again to get installer updates.

Update the RotorHazard plugin separately to the same release, then run **Play audio check**.

### Configuration and checks

Settings are in `/etc/default/sendspin-service`. Defaults: local HTTP API on `127.0.0.1:8766`, Sendspin player endpoint on `0.0.0.0:8927`. After editing, run `sudo systemctl restart sendspin-service`.

```shell
systemctl status sendspin-service --no-pager
curl --fail http://127.0.0.1:8766/health
journalctl -u sendspin-service -n 80 --no-pager
```

Expect `active (running)` and a JSON health response containing the service version.

## Docker Image

Use Docker Compose for optional cloud hosting. Run these commands on the cloud server from a checkout of this repository:

```shell
cp .env.example .env
sed -i "s/change-this-token/$(openssl rand -hex 32)/" .env
docker compose up -d
```

The Compose file builds from source. To use the published image, replace its `build:` block with `image: ghcr.io/dutchdronesquad/sendspin-service:latest`. Runtime settings are in `.env`.

To use the cloud service:

1. Set RotorHazard's **Sendspin service URL** to the cloud HTTP API base URL (port `8766` by default).
2. Open the cloud player at `http://<cloud-host>:8766/` and connect it to that server's Sendspin endpoint on port `8927`.
3. Run **Play audio check** in RotorHazard.

The plugin sends only to the selected server. Set its URL back to `http://127.0.0.1:8766` to use the local service again.

For a public deployment, set `SENDSPIN_API_TOKEN`; producers must send `Authorization: Bearer <token>` for `/v1/play` and `/v1/stop`. Keep the token unset only for local testing on a trusted machine.

The container includes its own player; install the RotorHazard plugin separately. Local and cloud servers on separate machines can use the same ports. If testing Docker on the Pi, avoid [port conflicts](#port-conflicts) with the `.deb` service.

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

This conflict applies only to overlapping ports on the same host. A `.deb` service on the RotorHazard Pi and a Docker service on a separate cloud host can both run. RotorHazard still sends to only the server selected in its settings.

If both deployments occupy the same host, choose which one should use the default ports:

- **Keep the `.deb` service on your Raspberry Pi:** run `docker compose down` from the directory of the Sendspin Compose project, then run `sudo systemctl restart sendspin-service`. For a container started with `docker run`, find it with `docker ps` and remove that Sendspin container with `docker rm -f <container-name>` (replace the placeholder with its actual name).
- **Keep Docker for your cloud deployment:** run `sudo systemctl disable --now sendspin-service`, then run `docker compose up -d` from the Sendspin Compose directory. Disabling the systemd service also prevents it from starting at the next boot.

Check `systemctl status sendspin-service --no-pager` and `docker ps` to confirm which deployment is running. If a port is still occupied, `sudo ss -ltnp '( sport = :8766 or sport = :8927 )'` shows the listeners. Verify the retained service with **Play audio check**.

Running both on the same host requires separate host ports for each deployment. Set RotorHazard and the intended playback clients to the same selected server; using separate ports does not enable parallel output from the plugin.

### Other issues

- **No audio in `/player`**: confirm `sendspin-service` is running, the player Server URL points at the same service RotorHazard sends to, and the player is connected.
- **Service unreachable**: confirm `curl http://127.0.0.1:8766/health` works from the RotorHazard host.
- **Outdated service**: compare the plugin release with `version` from `curl http://127.0.0.1:8766/health`. If they differ, reinstall or upgrade the component that does not match the intended Race Voice release.
- **Player page unreachable**: confirm `<RotorHazard UI base URL>/player` works from the playback device.
- **Some players hear different or duplicate audio**: verify that each player connects to the server selected by RotorHazard's **Sendspin service URL**. If multiple services run on the same host, give them distinct host ports. Check both `systemctl status sendspin-service` and `docker ps` on hosts where you have tested container deployments.
- **Duplicate voice callouts or tones**: set RotorHazard Voice Volume and Tone Volume to `0` in regular RotorHazard browser clients.
- **First phrase is slow**: the selected Piper model may still be downloading or loading.
- **Browser playback stutters**: test Safari or Chrome incognito with extensions disabled, then validate on the race network.
