# Changelog

All notable shipped changes to Race Voice should be documented in this file.

This changelog is intentionally concise. GitHub Releases can carry the fuller change list and release assets.

## [Unreleased]

### Medium-quality voices

High-quality models are no longer available for race callouts. Existing Cori High, Lessac High and Thorsten High selections automatically move to the corresponding Medium voice when RotorHazard starts or restores those settings. The new selection is saved and an operator notification asks for pre-cache preparation.

Update the RotorHazard plugin and primary voice service together. Run **Prepare pre-cache** before racing so the replacement model can be downloaded and warmed while internet access is available. Existing downloaded High models and caches are left on disk.

### Continuous race audio

Lap announcements now wait until the complete name, lap number and time are ready before starting local playback. This avoids a mid-sentence pause when time synthesis takes longer than the cached introduction. Announcements can start slightly later; reusable audio remains cached.

Cloud relay uploads available cached lap segments while the time is being prepared, then sends one complete announcement. This overlaps transfer with synthesis without publishing partial callouts.

Playback buffering now respects connected players that request more preparation time than the default queue limits. This prevents the Sendspin scheduler from inserting silence between chunks of a buzzer or spoken announcement. Minimum buffer requirements and output delay also contribute to startup timing.

### Automatic audio diagnostics

The voice service now automatically saves recent audio diagnostics while it runs. Nothing needs enabling before a race. Records stay on disk across service restarts, with a rolling limit of 64 MiB; the oldest data is replaced when that limit is reached.

After racing, run `sendspin-service diagnostics export` to create a ZIP in the current directory with a name such as `race-audio-2026-11-22-143000Z.zip` (UTC). Use `--output` only to choose a different name or location. Export keeps recording running and never overwrites an existing file.

The export includes synthesis, queue, relay and playback-scheduling measurements to help investigate delays and interruptions. Speech text and tokens are excluded. The browser player also automatically keeps its latest ten minutes of buffer and synchronization measurements, available as a separate download under **Diagnostics**. See the [capture guide](docs/latency-validation.md#capturing-a-baseline) for collection and interpretation limits.

## [2.2.0] - 2026-11-22

Race Voice 2.2.0 makes the standalone service easier to inspect and update on Raspberry Pi and upgrades the browser player to Sendspin JS 6. This release includes two changes to account for when upgrading.

### Upgrading to 2.2.0

- **Explicit service startup:** `sendspin-service` without arguments now shows help. Use `sendspin-service serve` to start a foreground server, with server options after `serve`. Update custom startup scripts and service units accordingly. The bundled systemd unit and Docker command already include this change.
- **Browser player compatibility:** the new player uses the Sendspin 1.0.0-rc1 protocol and requires an `aiosendspin` 10.x server. Update any older local or cloud service that browser listeners connect to before using the new player. Updating only the RotorHazard plugin does not update those services.

Existing server URLs and saved browser connection settings continue to work. Plugin and service release numbers do not need to match; the server's `aiosendspin` version determines browser protocol compatibility. See the [Usage Guide](docs/usage.md) for installation and upgrade instructions.

### Service management on Raspberry Pi

Inspect the service without accidentally starting a second server:

| Command | Purpose |
| --- | --- |
| `sendspin-service status` | Show the running service version, its `aiosendspin` version and connected player count. |
| `sendspin-service --version` | Show installed service, dependency and Python versions, even when the service is stopped. |
| `sendspin-service update` | Update a 64-bit Raspberry Pi OS/Debian `.deb` installation to the latest stable release. |

Status distinguishes the installed version from the running process, helping identify a service that still needs restarting after an update. It also supports remote service URLs and JSON output for scripts.

The updater shows the installed and target versions, asks for confirmation, verifies the downloaded package checksum, preserves configuration and restarts the service. An exact release can be selected with `update <release-tag>`; `--yes` confirms unattended updates. Docker installations continue to update by pulling and recreating their container. Existing relay commands remain available.

### Browser playback and compatibility checks

Disconnecting, leaving the player page or failing to connect now releases the browser player's audio resources instead of retaining an unused audio output.

Automated compatibility checks now cover both stable and development RotorHazard versions. Daily checks against the latest upstream stable release and development branch help detect new incompatibilities without waiting for a manual test-version update.

## [2.1.0] - 2026-10-29

### Faster lap announcements

**Prepare pre-cache** now warms the voice model even when all audio files already exist, and keeps reusable callsigns, lap numbers and countdown phrases in memory. The active heat's callsigns and lap numbers take priority in that cache.

With the model warm and the required segments in memory, local playback can begin the callsign and lap number while the new lap time is still being generated. The time follows within the same announcement, without another pilot speaking in between. Cloud relay continues to receive each complete announcement.

Run **Prepare pre-cache** before racing, including after a service restart or changes to pilots or voice settings. Live lap announcements also take precedence over queued pre-cache work. Audible latency still depends on the race hardware and connected players.

### More reliable race audio

- Each output keeps up to four waiting lap callouts in passage order, allowing four pilots crossing close together to be announced even when their combined speech exceeds four seconds. Under heavier load, newer laps replace older waiting calls for the same pilot or the oldest waiting lap. Expiry and memory limits still apply; new laps do not interrupt speech already playing.
- A normal race finish lets the winner and final callouts complete. **Stop audio**, race aborts, heat changes and staging the next race still clear old audio, including when racing the same heat again.
- Announcements wait for the service to acknowledge their race-state update, preventing a delivery race that could lose the winner announcement or cause an unnecessary reconnect.
- Cloud listeners can disconnect and reconnect during an audio check and resume at the current position, even when they were the only listener. Finished, stopped or cancelled audio is not replayed.

### Callsigns and minimum lap time

Lap announcements now prefer the pilot's **callsign**, falling back to the phonetic name when it is empty. Pre-cache preparation uses the same preference.

Laps below RotorHazard's **Minimum Lap Time** no longer generate speech or enter the audio queues. Laps exactly at the minimum are still announced, and setting changes apply on the next passage.

### Diagnostics and updating

Service logs now explain why callouts expire, are replaced, are cancelled or fail, and report whether the voice model and prepared heat segments are ready for early playback. Playback acceptance in the logs does not confirm sound at the speaker.

Update the RotorHazard plugin and primary Sendspin service to receive all changes. Update each separate playback or cloud relay service for the queue, diagnostics and reconnect fixes.

## [2.0.2] - 2026-10-25

### Clearer cache feedback

**Prepare pre-cache** and **Clear TTS cache** now report when they are queued, started and completed, including preparation totals or the number of files removed. Failures, cancellations and results that could not be confirmed produce separate alerts.

Pressing **Prepare pre-cache** while clearing is still in progress shows the active operation and its latest progress instead of suggesting that the service is disconnected. Wait for **TTS cache cleared** before preparing again. Preparation can also be queued while a connected service acknowledges a settings update.

### Faster commands and correct player versions

- Relay CLI commands respond faster by avoiding unnecessary audio/server startup work, especially on Raspberry Pi.
- The browser player bundled with a plugin release now displays the release version instead of `dev`.

Update the RotorHazard plugin for the cache notifications and player version display. Update the standalone Sendspin service to receive the CLI startup improvement.

## [2.0.1] - 2026-10-25

### Joining audio already in progress

The first listener can now join an audio check already in progress, including on a cloud relay that had no listeners when the track started. Playback joins the current position; finished or stopped audio is not replayed. **Stop audio** and race-context changes also cancel playback that is still waiting for its first listener.

Update the cloud service to receive this fix for cloud listeners. Updating only the local service or RotorHazard plugin does not update the cloud relay.

### Installation and compatibility

- Installing the plugin no longer requests a separate gevent version, avoiding unintended upgrades of RotorHazard's own dependency.
- The service now uses `aiosendspin` 10.x, with refreshed runtime and browser-player dependencies.
- Sendspin for Windows **2.2.6 is currently recommended**; the setup guide links directly to this release.

## [2.0.0] - 2026-09-16

Race Voice v2 moves the entire race-audio pipeline into the standalone Sendspin service; the RotorHazard plugin is now a thin adapter that only forwards race events and state. This is a hard cutover — update the plugin and service together. The in-process synthesis path and the old **Cloud Sendspin service URL** / **Cloud Sendspin API token** plugin fields are gone; see the [Usage Guide](docs/usage.md) for the three supported deployment shapes (local-only, local+relay, cloud-direct).

### Cloud relay redesigned

Cloud playback is now a relay from the primary service instead of a second upload from the plugin, so audio is only ever synthesized once. Stopping a race or changing heats now reaches the cloud side too, and a failed delivery retries automatically. Set it up with `sendspin-service relay enable --url <cloud-url>` instead of hand-editing the config file.

### Race-day reliability

- Scheduled stage tones and the race-start buzzer stream in chunks with client-aware lead time, fixing tones that played closer together than scheduled on slower connections.
- A stop or heat change discards synthesis still in flight instead of letting it play late.
- Announcements are prioritized over lap speech, with scheduled countdowns first.
- Lap synthesis load is bounded so a busy gate crossing cannot back up the pipeline.
- Piper/ONNX work is fully isolated from RotorHazard's gevent-patched event loop.

## [1.2.0] - 2026-09-13

### Cloud playback

Stream the same callouts and race sounds locally and to an authenticated cloud service simultaneously. Keep the local service URL and fill in **Cloud Sendspin service URL** and **Cloud Sendspin API token**. Separate queues keep cloud delays from holding up local audio; **Stop audio** acts on both outputs.

### Faster cloud audio

The audio check and race tones can play from files included with the cloud service, avoiding a full upload on each click. Repeated callouts reuse previously uploaded segments. Update both plugin and service for this optimization; older services retain the upload-based behavior. The complete test track is preserved, and local race audio keeps playing immediately.

### Sendspin reliability and diagnostics

- Large audio uploads, including the full audio-check track, are streamed to the service to avoid request-size failures.
- Each configured service is checked automatically in the background when RotorHazard starts. Successful checks are logged and problems produce warnings; **Play audio check** plays the test audio directly.
- Sendspin server names now include the hostname, making local and cloud services easier to distinguish in players.

## [1.1.0] - 2026-09-13

### ⚠️ Upgrade requirements

Requires **RHAPI 1.5 or newer**, currently available in RotorHazard 4.5.0-beta.1. RotorHazard 4.4.0 is unsupported. Update the Sendspin service to **1.1.0** for the new scheduled race sounds; service 1.0.0 lacks the required timing support.

### Race sounds and callouts

Adds staging beeps, a race-start buzzer, spoken reminders at 60, 30, and 10 seconds remaining, final-five-second tones, and an end-of-timer buzzer. **Rebuild pre-cache** includes the new phrases. Set RotorHazard browser Voice Volume and Tone Volume to `0` to avoid duplicate audio.

### Installation and maintenance

- The service installer adds release selection, checksum verification, and configuration-preserving updates. Local developers can build and install their checkout with `--dev`.
- Compatible plugin and service releases no longer need matching version numbers, removing unnecessary RH warnings.
- Updates the browser player to Sendspin JavaScript v5 and refreshes dependencies, including Piper TTS 1.8.0.

### Sendspin 9.x playback

Fixes service startup and browser connections with `aiosendspin` 9.x. The service keeps its server identity across restarts and admits browser players using the encrypted handshake. Existing unencrypted players remain supported.

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
