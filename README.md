<p align="center">
  <picture>
    <img alt="Race Voice" src="https://raw.githubusercontent.com/dutchdronesquad/rh-race-voice/develop/sendspin_player/public/favicon.svg" width="96">
  </picture>
</p>

<p align="center">
  <strong>Race-day voice callouts for RotorHazard, powered by Piper TTS and Sendspin.</strong>
</p>

<p align="center">
  <a href="https://github.com/dutchdronesquad/rh-race-voice/actions/workflows/linting.yaml"><img
    src="https://github.com/dutchdronesquad/rh-race-voice/actions/workflows/linting.yaml/badge.svg"
    alt="Linting"
  /></a>
  <a href="https://github.com/dutchdronesquad/rh-race-voice/actions/workflows/rhfest.yaml"><img
    src="https://github.com/dutchdronesquad/rh-race-voice/actions/workflows/rhfest.yaml/badge.svg"
    alt="RHFest"
  /></a>
  <a href="LICENSE"><img
    src="https://img.shields.io/badge/license-MIT-blue"
    alt="License"
  /></a>
</p>

<p align="center">
  <a href="https://github.com/dutchdronesquad/rh-race-voice/releases/latest"><strong>Download</strong></a>
  &middot;
  <a href="docs/usage.md"><strong>Usage Guide</strong></a>
  &middot;
  <a href="https://github.com/sendspin"><strong>Sendspin</strong></a>
  &middot;
  <a href="CONTRIBUTING.md"><strong>Contributing</strong></a>
</p>

<p align="center">
  Race Voice generates RotorHazard announcements on the timing server, caches reusable WAV files,
  and sends playback to a Sendspin service for network clients.
</p>

<p align="center">
  <img alt="Race Voice showcase" src="https://raw.githubusercontent.com/dutchdronesquad/rh-race-voice/develop/.github/assets/screenshot.png" width="800">
</p>

# Race Voice

Server-side voice callouts for the [RotorHazard] timing platform, powered by [Piper TTS]. Audio is generated on the RotorHazard server and sent to `sendspin-service`, which streams to connected clients using the [Sendspin] protocol.

## What you can do

- 🎙️ **Local TTS**: Generates voice callouts with [Piper TTS] on the RotorHazard server.
- 📡 **Sendspin service playback**: Sends generated WAV files to a service that streams PCM audio to connected Sendspin clients over WebSocket, including [WindowsSpin].
- 🌐 **Browser player**: A built-in RotorHazard plugin player at `/player` that connects to the Sendspin service.
- 🐳 **Optional cloud deployment**: Docker Compose is available as an extra for cloud hosting, including the browser player at `/`.
- 🔊 **Race sounds**: Plays staging tones and the race-start buzzer through the same Sendspin output path.
- 🎛️ **Configurable voice**: Adjustable speech speed, noise scale, and phoneme width from the RotorHazard settings panel.
- ⚡ **Smart caching**: Reusable pilot-name and lap-number segments are cached separately; use **Rebuild pre-cache** after first setup or voice model/settings changes to prepare them ahead of racing.

## Requirements

- [RotorHazard] with RHAPI support for `Evt.RACE_STAGE_TONE` and `Evt.RACE_CLOCK_CALLOUT`.
- Python 3.12 or newer.
- `sendspin-service` (installation below).
- Network access from playback clients to `sendspin-service`.
- A playback client: a browser or [WindowsSpin].

## Quick Start

**Recommended:** install the `.deb` service on the same Raspberry Pi as RotorHazard, running 64-bit Raspberry Pi OS.

1. Download `race_voice.zip` from the [latest release](https://github.com/dutchdronesquad/rh-race-voice/releases/latest), upload it in RotorHazard's plugin manager, and restart RotorHazard if requested.
2. On the RotorHazard machine, run:

   ```shell
   curl -fL https://github.com/dutchdronesquad/rh-race-voice/releases/latest/download/install-sendspin-service.sh -o install-sendspin-service.sh &&
     bash install-sendspin-service.sh
   ```

   Choose the same release as the plugin and confirm. The installer starts the service automatically. Stop any Sendspin Docker container on this machine first to avoid a [port conflict](docs/usage.md#port-conflicts).

3. In **Settings → Race Voice**, enable **Plugin audio** and keep **Sendspin service URL** at `http://127.0.0.1:8766`.
4. On your playback device, open `<RotorHazard UI base URL>/player` and press **Connect**. Alternatively, connect WindowsSpin to the Pi's LAN address on port `8927`.
5. Use **Play audio check** to test sound, then **Rebuild pre-cache** to prepare callouts.

Set RotorHazard browser **Voice Volume** and **Tone Volume** to `0` to prevent duplicate audio. The first use of a voice downloads its model.

[Docker Compose](docs/usage.md#docker-image) is an optional cloud setup with its own player. Select local or cloud using **Sendspin service URL**; the plugin sends to one server at a time.

## Documentation

- [Usage Guide](docs/usage.md): setup, settings, browser player, cache layout, operational notes, and troubleshooting.
- [Sendspin service installation](docs/usage.md#sendspin-service): package selection, installation commands, and playback from other devices on the LAN.
- [Changelog](CHANGELOG.md): release history.
- [Contributing](CONTRIBUTING.md): development setup and contribution guidelines.

## Sponsors

If Race Voice helps your club, event, or race-day workflow, you can help fund continued development and maintenance.

- Support the project through [GitHub Sponsors](https://github.com/sponsors/klaasnicolaas)
- Send a one-off contribution through [Ko-fi](https://ko-fi.com/klaasnicolaas)

## Contributing

Contributions are welcome! See [CONTRIBUTING.md](CONTRIBUTING.md) for setup instructions and development guidelines.

<a href="https://github.com/dutchdronesquad/rh-race-voice/graphs/contributors">
  <img src="https://contrib.rocks/image?repo=dutchdronesquad/rh-race-voice" alt="Contributors" />
</a>

## Credits

Race Voice uses [Sendspin] for synchronized network audio playback. Sendspin
and the browser SDK are Open Home Foundation projects; see
[sendspin-audio.com](https://www.sendspin-audio.com/) and
[openhomefoundation.org](https://www.openhomefoundation.org/).

The RotorHazard **Play audio check** button uses a bundled demo WAV so playback
can be tested without generating TTS first. That check clip is:

- Music track: Foreign by Moavii
- Source: <https://freetouse.com/music>
- Free Music Without Copyright (Safe)

## License

Distributed under the **MIT** License. See [`LICENSE`](LICENSE) for more information.

<!-- LINKS -->
[RotorHazard]: https://github.com/RotorHazard/RotorHazard
[Piper TTS]: https://github.com/OHF-Voice/piper1-gpl
[Sendspin]: https://github.com/sendspin
[WindowsSpin]: https://github.com/sendspin/windowsspin

[license-shield]: https://img.shields.io/github/license/dutchdronesquad/rh-race-voice.svg
[maintenance-shield]: https://img.shields.io/maintenance/yes/2026.svg
[project-stage-shield]: https://img.shields.io/badge/project%20stage-experimental-yellow.svg
[rhfest-shield]: https://github.com/dutchdronesquad/rh-race-voice/actions/workflows/rhfest.yaml/badge.svg
[rhfest-url]: https://github.com/dutchdronesquad/rh-race-voice/actions/workflows/rhfest.yaml
