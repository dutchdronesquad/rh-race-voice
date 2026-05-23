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
- 🐳 **Container**: A Docker image is available for standalone service deployments, including the browser player at `/`.
- 🔊 **Race sounds**: Plays staging tones and the race-start buzzer through the same Sendspin output path.
- 🎛️ **Configurable voice**: Adjustable speech speed, noise scale, and phoneme width from the RotorHazard settings panel.
- ⚡ **Smart caching**: Reusable pilot-name and lap-number segments are cached separately; use **Rebuild pre-cache** after first setup or voice model/settings changes to prepare them ahead of racing.

## Requirements

- [RotorHazard] with RHAPI support for `Evt.RACE_STAGE_TONE`.
- Python 3.12 or newer.
- `sendspin-service` installed on the RotorHazard host or another reachable machine.
- Network access from playback clients to `sendspin-service`.
- A browser on the playback device. RotorHazard serves the Sendspin player at `<RotorHazard UI base URL>/player`.

## Quick Start

1. Download `race_voice.zip` from the latest GitHub release.
2. In RotorHazard, open the plugin manager and upload the ZIP file.
3. Restart RotorHazard if requested.
4. Download the matching `sendspin-service_*.deb` from the same GitHub release and install it on the RotorHazard host.
5. Open the RotorHazard settings page and enable **Race Voice**.
6. Confirm **Sendspin service URL** points to the service, normally `http://127.0.0.1:8766`.
7. Open `<RotorHazard UI base URL>/player` from the playback device.
8. Use **Rebuild pre-cache** to prepare schedule, pilot-name, and lap-number WAV files.
9. Use **Generate test phrase** or **Play audio check** to verify playback.

Set RotorHazard browser Voice Volume and Tone Volume to `0` on clients that should only use Race Voice audio.

The first generated phrase for a voice model downloads the Piper model into the RotorHazard data cache. That can take a moment depending on the server and network connection.

## Documentation

- [Usage Guide](docs/usage.md): setup, settings, browser player, cache layout, operational notes, and troubleshooting.
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
