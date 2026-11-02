# Contributing

Thank you for your interest in contributing to Race Voice!

## Requirements

You need the following tools to get started:

- [uv] — Python virtual environment and package manager
- [Python] 3.12 or higher
- [Node.js] 20 or higher (for the browser player)

## Setup

1. Clone the repository.
2. Install Python dependencies:

```bash
uv sync --all-groups
```

3. Install browser player dependencies:

```bash
cd sendspin_player && npm install
```

## RotorHazard Development Install

When installing the plugin from a ZIP file, RotorHazard installs the manifest dependencies automatically. During local development, install those packages into the RotorHazard virtual environment yourself.

With the RotorHazard venv active:

```bash
uv pip install piper-tts aiosendspin av numpy pillow
```

To update those packages in the RotorHazard venv:

```bash
uv pip install --upgrade piper-tts aiosendspin av numpy pillow
```

## Local service installation

On a supported systemd machine with `python3`, `uv`, and `nfpm` available, run `bash tools/install-sendspin-service.sh --dev` from this checkout to build and install your local service changes. Repeating the command rebuilds and reinstalls the development package. See [Package Build](docs/usage.md#package-build) for requirements and returning to a stable release.

## Pre-commit checks

This repository uses the [prek] framework. All changes are linted and tested on each commit.

Install the pre-commit hook:

```bash
uv run prek install
```

Run all checks manually:

```bash
uv run prek run --all-files
```

Run checks on staged files only:

```bash
uv run prek run
```

## Browser Player

The Sendspin browser player source lives in `sendspin_player/` and is built with Vite + React + shadcn/Radix.

```bash
cd sendspin_player
npm run dev      # local dev server
npm run lint     # lint with Oxlint
npm run build    # standalone production build at /
npm run build:plugin # RotorHazard plugin production build at /player/
```

Both build commands run the TypeScript build and write production output to `custom_plugins/race_voice/player/`. Use `npm run build:plugin` before packaging the RotorHazard plugin. The `assets/` subdirectory is not committed to git and must be built locally before deploying.

> **Browser compatibility note**: During local testing, Safari on macOS gave the smoothest playback experience. Chrome on macOS occasionally showed small playback interruptions and more sync-error movement, particularly with browser extensions active. When testing sync quality in Chrome, use an incognito window with extensions disabled before treating jitter as a server-side issue.

## RotorHazard contract tests

The `RotorHazard contract tests` CI job loads the actual upstream RHAPI, event manager, filters and RHUI phonetic emitters at the revision recorded in `tests/rotorhazard.json`. Only persistence, configuration and browser socket emission use test doubles. These tests run in a separate gevent-patched interpreter and do not start RotorHazard hardware or play audio.

To run locally, check out that exact revision of `RotorHazard/RotorHazard`, then run:

```sh
uv sync --all-groups --extra sendspin-service
RACE_VOICE_RH_SOURCE=/path/to/RotorHazard uv run python -m unittest tests.test_rh_contract tests.test_rh_sendspin_chain -v
```

The source must be unmodified. Without `RACE_VOICE_RH_SOURCE`, the ordinary suite skips this optional integration test; the dedicated CI job supplies the checkout and fails for a missing or mismatched revision. Updating the pin is an explicit compatibility check, not an automatic tracking of upstream `main`. This verifies the pinned version, rather than claiming coverage of every release allowed by the plugin's API minimum.

The Sendspin chain test runs real RH events and phonetic emitters in the gevent subprocess, the plugin's HTTP publisher, the service application and admission/clock handshake, synthesis worker pipes and caches, output planners, and an encrypted connection to an actual SDK player. Only native inference returns synthetic WAV segments. Each phrase has a distinct PCM marker, so the receiving client can check exact segment order, sample count and content for four pilots followed by a winner. Other assertions cover `lap=None`, subminimum laps, cancellation reaching the client, and fresh audio after cancellation. PCM fixtures use 24 kHz to avoid fractional sample-to-microsecond rounding at chunk boundaries; this chain does not exercise resampling.

These tests validate software delivery, not intelligibility of a Piper model, physical speaker wake-up or Raspberry Pi latency. They open ephemeral loopback ports, disable mDNS, never open an audio device, and do not use a running event service. Hardware performance still needs occasional event-system measurements.

<!-- LINKS -->
[uv]: https://docs.astral.sh/uv/
[Python]: https://www.python.org/
[Node.js]: https://nodejs.org/
[prek]: https://prek.j178.dev/
