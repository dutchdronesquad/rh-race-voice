# Race Voice Callouts Plugin — Plan of Approach

## Current Implementation Note

The plugin has moved to the service-owned Sendspin model. It generates/caches WAV files and sends inline WAV payloads to `sendspin-service` over HTTP. RotorHazard should not start an internal Sendspin server in the normal runtime path.

Older sections below still describe design history and deferred options. Treat the current implementation as:

```text
RotorHazard plugin -> Piper TTS/WAV cache -> HTTP output -> sendspin-service -> Sendspin players
```

## Problem

RotorHazard uses browser TTS for spoken race callouts. In Chrome, some voices are remote services that silently fail when the race network has no internet access, while beeps and MP3 sounds continue to work. The goal is to move spoken callout generation to a fully local speech service running server-side.

## Concrete Goal

An RHAPI-only RotorHazard plugin that:

- Listens to race events on the RotorHazard host (server-side Python only).
- Builds its own audio queue for voice callouts and indicator sounds.
- Uses Piper as the local TTS engine to generate speech fully offline.
- Caches generated WAV files so predictable race phrases do not need synthesis during critical moments.
- Sends audio to a Sendspin player on the local network as the primary output target.
- Requires no RotorHazard core changes.

**Not in scope:** replacing browser-finalized lap callouts (assembled in JS), injecting JavaScript into existing pages, or voice command input.

**Minimum Python version:** 3.12 (required by Sendspin).

---

## RHAPI Limitations

- Lap callouts are assembled in browser JS from `phonetic_data` using per-browser localStorage settings. A server-side plugin cannot intercept the final phrase.
- The plugin cannot edit the built-in Audio Control tab or disable browser TTS automatically on other clients.
- The plugin cannot inject JavaScript into existing RotorHazard pages.

Consequence: **duplicate prevention is operational**, not automatic. The operator must manually set Voice Volume to 0 on all regular RotorHazard browser clients when the plugin is active.

---

## Missing RH Features (Post-MVP wishlist for upstream)

Things that would make the plugin significantly easier or more capable, but don't exist in RotorHazard today. These are explicitly **Post-MVP** items: they are not required for Phase 2 or Phase 3 completion. Each item includes the ideal upstream fix and the current workaround.

### Plugin-ready event after settings initialization

**Problem:** External plugins can miss `Evt.STARTUP` because it fires before user plugins are loaded in some RotorHazard startup paths. Plugins that need database-backed UI settings after `init_setting_defaults()` do not have a reliable server-side event for safe initialization.

**Ideal fix:** Add a post-plugin-load event such as `Evt.PLUGINS_READY` or `Evt.SETTINGS_READY`, fired after plugin loading and registered setting defaults are initialized. This gives plugins a stable point to read options and start background work.

**Status: Post-MVP / deferred.** Race Voice does not automatically build startup pre-cache until RH exposes a reliable lifecycle event. Operators can use **Rebuild pre-cache** after startup.

---

### Race clock countdown events

**Problem:** No server-side events for race time warnings ("30 seconds remaining", "10 seconds remaining", etc.). The countdown is handled entirely in browser JS via a local timer.

**Ideal fix:** Add `Evt.RACE_CLOCK_WARNING` fired by the RH race thread at configurable thresholds (e.g. 60s, 30s, 10s remaining), with payload `{'seconds_remaining': int}`. This would let any plugin — not just audio plugins — react to race time milestones without reimplementing a parallel timer.

**Status: Post-MVP / deferred.** Not implemented in the plugin until RH provides a proper server-side event. Race clock callouts are skipped for now.

---

### Staging tone events

**Problem:** The staging beeps ("3... 2... 1...") before race start are generated in browser JS. There is no `Evt.RACE_STAGE_TONE` or similar server event. Plugin cannot reproduce the countdown sequence without reimplementing the staging logic.

**Ideal fix:** Fire `Evt.RACE_STAGE_TONE` from `RHRace.stage()` at each staging beep interval, with payload `{'tone_index': int, 'tones_remaining': int}`. This decouples the audio signal from the browser and lets server-side plugins (LED, audio, video) stay in sync with the actual staging sequence.

**Status: Post-MVP / deferred.** Not implemented in the plugin until RH fires server-side arm tone events. Staging beeps are skipped for now.

---

### Structured race-tied / overtime events

**Problem:** Race Tied and Overtime announcements go through `emit_phonetic_text()` with translated strings. There is no dedicated `Evt.RACE_TIED` or `Evt.RACE_OVERTIME`. The plugin must either hook `Flt.EMIT_PHONETIC_TEXT` and parse translated text (fragile), or inspect `win_status` inside `Evt.RACE_WIN`.

**Ideal fix:** Fire dedicated `Evt.RACE_TIED` and `Evt.RACE_OVERTIME` events from `check_win_condition()` in addition to the existing `Evt.RACE_WIN`. This mirrors the pattern already used for `Evt.RACE_PILOT_DONE` vs `Evt.RACE_WIN` and makes win outcome handling explicit for plugins.

**Status: Post-MVP / deferred.** Not implemented until RH fires dedicated events. Race Tied and Overtime callouts are skipped for now.

---

### Race leader callout via unified filter

**Problem:** `Evt.RACE_PILOT_LEADING` fires correctly, but the phonetic text is assembled in `emit_phonetic_leader()` and emitted as a separate `phonetic_leader` socket event — bypassing `Flt.EMIT_PHONETIC_TEXT`. Plugins that want to intercept all voice callouts must register a second filter (`Flt.EMIT_PHONETIC_LEADER`) separately.

**Ideal fix:** Route `emit_phonetic_leader()` through `emit_phonetic_text()` with a `domain='race_leader'` parameter, just like winner announcements use `domain='race_winner'`. This unifies all voice output behind a single filter point and makes `Flt.EMIT_PHONETIC_TEXT` truly the single interception point for audio plugins.

**Status: Post-MVP / deferred.** Not implemented until RH routes leader callouts through the unified `Flt.EMIT_PHONETIC_TEXT` filter. Race leader callouts are skipped for now.

---

## A. Local TTS Engine

### piper-tts Python package (MVP)

Install `piper-tts` from PyPI directly into the RotorHazard virtualenv. Call it in-process — no subprocess, no binary to locate.

```python
from piper import PiperVoice

voice = PiperVoice.load("en_GB-alan-medium.onnx")
with wave.open("out.wav", "wb") as f:
    voice.synthesize("Pilot 1, Lap 3", f)
```

**Pro:** no subprocess startup overhead, no binary path to manage, same install path as other Python dependencies.
**Model download:** the plugin downloads the selected voice model automatically from the Hugging Face `rhasspy/piper-voices` repository on first use (or when the model is changed). Models are cached in `~/rh-data/race_voice_cache/models/`. Internet is only needed once per model; race operation is fully offline after that.

**Supported languages:** English, Dutch, and German to start. Recommended default models: `en_GB-alan-medium` (English) and `nl_NL-mls-medium` (Dutch). The plugin setting shows a dropdown of available models; the operator picks one per installation.
**Mitigation for synthesis latency:** cache generated phrases by normalized text and synthesis parameters. The plugin pre-generates predictable pilot-name segments and lap-number segments. Real-time synthesis remains necessary for dynamic lap times and unexpected text.

### Wyoming Piper (upgrade path)

Piper as a persistent TCP service. Plugin sends text, receives WAV back. Can run on a separate LAN machine if the Pi is underpowered for synthesis.

**Recommendation:** start with the `piper-tts` Python package + WAV caching. Add Wyoming Piper as an optional backend in Phase 5.

---

## B. Audio Output — Sendspin

Sendspin separates a **server/source** (orchestrates streams, accepts audio input) from a **player/client** (receives audio, plays through a local audio device). The current development branch has proven both embedded and service-based playback, but the target direction is a packaged local Sendspin service that runs outside RotorHazard. Named player targeting remains planned work.

```
RotorHazard plugin
  → generates WAV via Piper
  → Sendspin server/source
  → Sendspin protocol over LAN
  → Sendspin player (any device with a speaker: NUC, laptop, Pi, ...)
  → speaker / mixer
```

Updated direction: local Sendspin playback should use a separate packaged
service, not an internal plugin-managed server. The detailed service packaging,
runtime, `.deb`, and deployment plan lives in `Sendspin Service Package PVA.md`.
"Sidecar" is only an internal/deployment shorthand; user-facing docs should
prefer **Sendspin service**, **Local Sendspin service**, and **Cloud Sendspin
service**.

The Sendspin service can run locally on the RotorHazard host or in the cloud.
The player runs on whatever device is connected to the speakers. These are
deployment choices, not separate callout-generation code paths.

Target selection should support fan-out. The plugin may send the same generated WAV job to both an external Sendspin server for the PA and a cloud Sendspin server for phones. The local PA target must not be delayed by a slow or unreachable cloud target.

### Sendspin server placement

**Legacy: Internal Sendspin server on the RotorHazard host**

The original MVP path. The plugin drove `aiosendspin` directly in the
RotorHazard process. This proved useful for initial validation, but it adds
mode-switching complexity, competes for port `8927`, and couples Sendspin
restarts to RotorHazard restarts. This path is superseded by the packaged local
Sendspin service plan.

Migration note: the plugin-side internal implementation has now been removed.
Local playback goes through the packaged service HTTP API.

```
RotorHazard plugin → internal Sendspin server → Sendspin player
```

**Local Sendspin service on the Raspberry Pi**

Preferred local path. Sendspin runs as a separate packaged service managed by
systemd. The plugin keeps generating and caching WAV files, but sends playback
jobs to the service over local HTTP. The target install is a self-contained
`.deb` package so users do not need to manage `uv`, `pip`, virtualenvs, or
Python versions.

```
RotorHazard plugin → http://localhost:8766 → Sendspin server → Sendspin player
```

**In the cloud**

The same Sendspin server runs on a VPS, Docker host, or Sendspin-hosted environment. The plugin sends audio jobs over the internet, protected by token auth. Anyone at the event can scan a QR code and hear callouts on their own phone — no dedicated audio device or PA needed.

```
RotorHazard plugin → HTTPS → Cloud Sendspin server → phone (QR code)
```

This works as the only audio path for small events, or in parallel with a local player for events that have both a PA and a spectator feed.

**Local + cloud fan-out**

For full events, the plugin should be able to send every playback job to both targets:

```
RotorHazard plugin
  → Local Sendspin service   → PA / speakers
  → Cloud Sendspin service   → phones / spectator feed
```

Fan-out behavior:
- Local and cloud sends run independently.
- Local target uses a short timeout and remains the race-day priority.
- Cloud target uses its own timeout and failure state.
- A failed cloud send is logged and surfaced in status, but must not block or delay the local PA.
- Stop commands fan out to every enabled target.

### Player placement

The Sendspin player can run on any device with an audio output: Intel NUC, laptop, Raspberry Pi, phone. The operator installs the Sendspin player daemon, names it (e.g. `Race Speakers`), and the plugin targets that name. Multiple players can be active simultaneously.

### Deployment examples

| Situation | Sendspin service | Player |
|---|---|---|
| Normal local setup | Local service package on Pi | NUC or laptop at speakers |
| Small event, no PA | Cloud | Pilots' and spectators' phones via QR code |
| Full event setup | Local service for PA + cloud service for phones | NUC at PA + any phone |


### Browser player: current implementation

The browser player is a Vite / React / TypeScript app using shadcn/Radix UI
components and the official `@sendspin/sendspin-js` SDK. The player should not
own WebAudio timing, buffering, correction, reconnect logic, or Sendspin
protocol details; those remain SDK responsibilities.

The browser player source now lives in the root-level `sendspin_player/` app.
The previous root-level `player/` source directory has been removed in this
migration. Build, Docker, release, and developer documentation all point at
`sendspin_player/`. Production assets still build into
`custom_plugins/race_voice/player/` so the RotorHazard plugin can serve
`/player` from the plugin ZIP.

#### Source and build output

```text
rh-race-voice/
  sendspin_player/                 # Vite/React/shadcn source app
    package.json
    package-lock.json
    vite.config.ts
    tsconfig.json
    index.html
    src/
      index.tsx
      index.css
  custom_plugins/race_voice/player/
    index.html                    # generated during plugin release build
    assets/...                    # generated during plugin release build
```

#### Current design

- Vite + React + TypeScript.
- shadcn/Radix components for buttons, dialogs, fields, selects, sliders, and collapsible sections.
- Router: **No**. The app is a single runtime page served at `/player`; routing adds no value.
- Prerender / SSG: **No**. The app depends on WebSocket, WebAudio, user gestures, and runtime Sendspin state.
- ESLint: **Yes**. Realtime audio and connection-state code benefits from catching simple mistakes early.
- Build output: `custom_plugins/race_voice/player/`.
- Plugin route: `GET /player` returns the built `index.html`; static assets are served from the same directory.

Design note: use real shadcn/Radix primitives, not local lookalike component
classes. Custom CSS should be limited to the shadcn theme tokens, base styles,
and player-specific visuals that are not generic UI controls, such as the
status-ring animation and QR code sizing.

#### Runtime dependency

```json
{
  "dependencies": {
    "@sendspin/sendspin-js": "^3.1.0"
  }
}
```

The app instantiates `SendspinPlayer` from `@sendspin/sendspin-js` and lets the SDK handle:

- Sendspin protocol handshake
- time sync
- reconnect behavior
- decoding / scheduling
- drift correction / resync
- volume and mute state

Player construction follows this shape:

```ts
new SendspinPlayer({
  baseUrl,
  playerId,
  clientName: "Race Voice Browser Player",
  codecs: ["pcm"],
  correctionMode,
  reconnect: {
    baseDelayMs: 1000,
    maxDelayMs: 15000,
    maxAttempts: Infinity,
  },
  onStateChange: updateUiFromSdkState,
});
```

`baseUrl` should be a HTTP(S) origin, not the websocket endpoint path. The UI accepts values like `localhost:8927`, `http://host:8927`, or `ws://host:8927/sendspin`, and normalizes them to the SDK `baseUrl` form before constructing `SendspinPlayer`.

#### Browser UI

- Connect / disconnect button.
- Server URL input, persisted in `localStorage`.
- Volume slider and mute toggle.
- Correction mode selector.
- Status states: disconnected, connecting, connected, playing, error; reconnect attempts are logged and shown as connecting.
- Compact sync diagnostics from `player.syncInfo` and `player.timeSyncInfo` for debugging browser hiccups.
- Activity log for connection events and SDK state changes.

The browser player should continue to avoid local audio scheduling code. Timing, correction, reconnects, and decoding belong in the Sendspin SDK.

Ownership note: the RotorHazard plugin owns the local browser player because it is part of the race-control UI and sharing it is an operator decision. Docker/cloud Sendspin deployments can still bundle a service-owned player later for QR/session flows.

#### Build / dev scripts

```json
{
  "scripts": {
    "dev": "vite --host 0.0.0.0",
    "build": "tsc --noEmit && vite build",
    "check": "tsc --noEmit",
    "lint": "eslint .",
    "preview": "vite preview --host 0.0.0.0"
  }
}
```

`vite.config.ts` uses a small `fromConfig()` helper and writes production output to `custom_plugins/race_voice/player`.

#### Validation checklist

- [x] `npm run check` passes in the current player app.
- [x] `npm run lint` passes in the current player app.
- [x] `npm run build` emits `custom_plugins/race_voice/player/index.html` and assets.
- [x] RotorHazard plugin serves `/player` and static player assets.
- [x] Hard refresh / clean localStorage computes a usable default URL from the current host.
- [x] Browser player connects to `aiosendspin` on `8927`.
- [x] Test phrase plays in sync with a native Sendspin player.
- [x] Stop button clears playback on native and browser players.
- [x] MacBook/browser hiccup does not create persistent delay after recovery.
- [x] Reconnect after Sendspin server restart works without page refresh.
- [x] Retire old `player/` source path from Docker, CI/release workflows, and developer docs.
- [x] Remove old root-level `player/` source directory as part of the player migration.

### Known Sendspin risks to validate before race day

- **Public preview:** APIs may still change.
- **Browser/network hiccups:** validate the `/player` browser client on the actual race network and speaker device before relying on it for an event.

---

## Architecture

```
RotorHazard server
  └── Race Voice Plugin (Python 3.12+, RHAPI)
        ├── Event listeners
        │     Evt.HEAT_SET, CROSSING_ENTER/EXIT
        ├── Flt.EMIT_PHONETIC_DATA  (lap data snapshots)
        ├── Flt.EMIT_PHONETIC_TEXT  (server-originated text callouts)
        ├── services/lap_callouts.py (lap callout segment planning)
        ├── services/precache.py    (manual pre-cache rebuilds)
        ├── services/schedule.py    (scheduled-race countdown timers)
        ├── Async audio queue (FIFO, priority levels)
        ├── TTS: piper-tts (in-process) → WAV cache
        │     ~/rh-data/race_voice_cache/tts/
        └── Sendspin output
              Local: HTTP client → local Sendspin service
              Cloud: HTTP client → cloud Sendspin service
              Fan-out: target dropdown selects local, cloud, or both
```

Updated Sendspin output direction:

- The plugin should only dispatch WAV playback jobs over HTTP.
- The local Sendspin service owns `aiosendspin`, player connections, and stream state.
- The detailed package/build/deploy plan is tracked in `Sendspin Service Package PVA.md`.
- Cloud output uses the same service API and can run alone or in parallel with
  the local target.

The audio queue should remain unchanged: it still owns priority, expiry, and
single-worker ordering. Output fan-out happens behind the queue worker. Each
target is attempted independently with the configured request timeout; failure
on one target is logged and does not prevent attempts to other selected targets.

The local Sendspin service should expose a minimal ingest API:

- `GET /health` — server running, Sendspin status, connected player count when available.
- `POST /v1/play` — accepts one or more WAV files plus optional expiry metadata; queues/appends them to the Sendspin stream.
- `POST /v1/stop` — stops current playback and clears scheduled audio.

The same server implementation should be usable for a local systemd unit and a cloud Docker container. Cloud deployments additionally require TLS at the edge and token authentication for ingest endpoints.

### Playback worker

- Single async worker, FIFO queue.
- Priority: Race Start / Winner / Interrupt > lap callouts > crossing beeps.
- Callouts expire after ~5 seconds — a stale lap callout should never play.
- Sendspin output appends queued WAVs to the active stream so normal lap callouts do not reset playback when another lap arrives.
- All synthesis is async; never blocks RotorHazard event handling.

### Audio cache

Cache path: `{model_name}/{sha1(normalized_text)}_{speed}_{noise}_{noise_w}.wav`

Current heat-load behavior:
- Clears ephemeral lap-time WAV files for the selected model.
- Pre-cache generation is manual via **Rebuild pre-cache** until RH provides a reliable plugin-ready lifecycle event.
- Rebuild pre-cache generates schedule phrases, current-heat pilot-name segments, and lap-number segments under `tts/<model>/precache/`.

---

## Integration with Built-In RotorHazard Audio

The plugin cannot take over the built-in Audio Control tab. The operating model is:

1. Install and enable the plugin.
2. Configure Piper in the plugin panel.
3. Run a Sendspin player on the device connected to the speakers and connect it to the RotorHazard host on port `8927`.
4. On every regular RotorHazard browser client: set Voice Volume to 0 and disable browser beeps if the plugin handles beeps.

**Built-in Audio Control → used only to silence browser clients**
**Plugin Audio Profile → planned place to decide what the speakers announce**

MVP plugin audio profile mirrors the familiar RH categories that can be implemented with current RHAPI hooks:
- Pilot callsign, lap number, lap time on/off
- Winner and pilot-finished callouts
- Crossing enter/exit beeps
- Voice volume, beep volume, speech speed, voice model

Post-MVP profile additions:
- Race clock callouts
- Staging tone beeps
- Race tied / overtime callouts
- Race leader callouts

---

## Plugin Settings

Implemented:
- Enable plugin audio: on/off
- Voice model selector: 12 models across English, Dutch, and German
- Speech speed, noise scale, phoneme width noise
- Sendspin target dropdown: Local / Cloud / Local + Cloud
- Local Sendspin URL and optional API token
- Cloud Sendspin URL and optional API token
- Test phrase field and quick button
- Play audio check quick button
- Stop audio quick button
- Clear TTS cache quick button
- Duplicate prevention warning for browser Voice Volume
- Model download/load status surfaced to UI as notifications

MVP planned / not implemented yet:
- Announcement options per component: Pilot Callsign, Pilot Lap Number, Pilot Lap Time — each selectable as Never / Always / Only on Non-Team/Non-Co-op Races. Use Python enums for option values.
- Output volume and beep volume in the plugin panel
- Pre-generate on heat load: on/off

Known issues / deferred fixes:
- Sendspin reconnect silence: after a client reconnects, no audio is heard until the next new stream; likely a stream state issue in `SendSpinServer`
- Speed setting effectiveness: speed=2.0 produces little noticeable change; investigate whether `length_scale` is being applied correctly or whether medium-quality Piper models simply have a narrow effective range

Post-MVP planned / not implemented yet:
- TTS engine selector: piper-tts / Wyoming Piper / disabled
- Wyoming Piper host and port
- Sendspin output target health/status display:
  - Local Sendspin service health status
  - Cloud Sendspin service health status
- Sendspin server target timeouts:
  - Local service timeout: short default, e.g. 2-5 seconds
  - Cloud service timeout: longer default, e.g. 3-5 seconds
- Sendspin cloud join URL / QR code

---

## MVP Implementation Phases

The MVP is Phase 1 through Phase 3: local Piper synthesis, race-callout queueing, and local Sendspin/browser playback. A phase is done when every checkbox is ticked and the success criteria are met — not before.

Deferred RHAPI-dependent features, external/cloud Sendspin output, QR codes, Wyoming Piper, mDNS, and broader polish are tracked separately under **Post-MVP Phases**.

---

### Phase 1 — Foundation: Plugin Scaffold + Piper TTS

**Goal:** A working plugin that generates a valid WAV file from text using the `piper-tts` Python package. Proves the TTS pipeline and cache end to end.

#### Plugin structure
- [x] Create `custom_plugins/race_voice/__init__.py` with `initialize(rhapi)` entry point
- [x] Register a settings panel via `rhapi.ui.register_panel()`
- [x] Add "Enable plugin audio" toggle via `rhapi.fields.register_option()`
- [x] Add voice model selector setting (12 models: EN-GB, EN-US, NL, DE)
- [x] Add speech speed setting
- [x] Add "Test phrase" quick button via `rhapi.ui.register_quickbutton()`

#### piper-tts integration
- [x] Add `piper-tts` to plugin dependencies
- [x] On first use (or model change): auto-download selected voice model from Hugging Face into `~/rh-data/race_voice_cache/models/`
- [x] Handle download failure with a clear error message (status logged; no panel field — see note below)
- [x] Load `PiperVoice` lazily on first synthesis; reload when the model changes
- [x] Synthesize text to WAV using `voice.synthesize_wav(text, wav_file, syn_config)` (piper-tts >= 1.4 API)
- [x] Handle synthesis errors gracefully (log, skip, no crash)

#### Audio cache
- [x] Create cache directory: `~/rh-data/race_voice_cache/tts/`
- [x] Cache key: `{sha1(normalized_text)}_{speed}_{noise}_{noise_w}.wav` under a per-model directory
- [x] On cache hit: return existing WAV path, skip synthesis
- [x] On cache miss: synthesize, write to cache, return WAV path

#### Event hook
- [x] Register `Flt.EMIT_PHONETIC_TEXT` handler
- [x] Schedule intercepted callout text for synthesis/playback to verify hook fires correctly
- [x] Hook does not modify or block the callout payload

#### Status display
- [x] Status logged via RotorHazard logger (visible in RH log panel)

**Success criteria:**
- [x] Clicking "Test phrase" generates a WAV in the cache directory with correct size and a valid WAV header
- [x] Second click on "Test phrase" returns the cached file without re-synthesizing (log confirms cache hit)
- [x] `Flt.EMIT_PHONETIC_TEXT` handler is registered and schedules text callouts without modifying the payload
- [x] Model not found / download failure logs a clear status and does not crash RotorHazard

---

### Phase 2 — Race Events + Audio Queue

**Goal:** Lap/text callout filters are wired. Async queue has priority and expiry. Pilot phrases can be pre-cached on demand.

#### Current callout inputs
- [x] `Flt.EMIT_PHONETIC_DATA` → "Pilot [callsign], Lap [n]" + optional lap time
- [x] `Flt.EMIT_PHONETIC_TEXT` → server-originated text callouts via TTS
- [x] Winner callouts are covered and manually validated through `Flt.EMIT_PHONETIC_TEXT` (`domain='race_winner'`, `winner_flag=True` gets high priority)
- [x] `Evt.HEAT_SET` → clear queued audio and ephemeral lap-time WAVs
- [x] **Rebuild pre-cache** quick button → pre-cache schedule phrases, current-heat pilot-name segments, and lap-number segments on demand
- [x] `Evt.DATABASE_RESET` → clear event-specific `tmp/` and `precache/` WAVs after Archive/New Event or Clear Data Only

#### Async audio queue
- [x] Single `queue.PriorityQueue` with a dedicated daemon worker thread (`audio_queue.py`)
- [x] `AudioJob` dataclass: `text`, `wav_paths`, `priority`, `expires_at`
- [x] Priority levels: `HIGH` (race start / winner / interrupt) > `NORMAL` (lap callout) > `LOW` (beep)
- [x] Expiry: drop jobs where `time.monotonic() > expires_at` (default: 5 seconds)
- [x] Lap callouts use a 10-second expiry to tolerate multiple pilots crossing together while still dropping stale audio
- [x] Single worker draining the queue in priority order; expired jobs dropped and logged
- [x] Sendspin appends queued WAVs to the active stream; normal lap callouts do not intentionally stop/reset current playback

#### Audio profile settings
- [x] All implemented toggles exposed in plugin settings panel

#### Cache handling
- [x] Hook into `Evt.HEAT_SET`
- [x] Clear ephemeral lap-time WAV files when a heat loads
- [x] **Rebuild pre-cache** pre-synthesizes pilot-name and lap-number segments for the current heat
- [x] Pre-caching runs in the synth thread pool; does not block event handling
- [x] Log how many new WAVs were generated and how long it took

#### Duplicate prevention UI
- [x] Plugin panel shows two separate markdown warnings (Voice Volume + browser beeps)

#### Error handling
- [x] Piper fails → log error with phrase text, skip callout, continue
- [x] Event handler never raises an unhandled exception
- [x] No race event is delayed or dropped because of plugin audio work

**Success criteria:**
- [x] A full simulated race (stage → start → laps → winner → stop) produces the correct callouts in the log
- [x] Pilot-name and lap-number segments are pre-generated by **Rebuild pre-cache**; generation time logged
- [x] Piper crash mid-race does not affect RotorHazard timing or results
- [x] Expired callouts are dropped and logged, never played late

---

### Phase 3 — Sendspin Integration

**Goal:** Audio leaves the Pi and plays on a remote browser player via Sendspin. Local Pi playback is retired as the primary path.

#### Sendspin source setup
- [x] Add `aiosendspin` to plugin dependencies; keep server-only extras explicit in `manifest.json` (`av`, `numpy`, `pillow`) because RotorHazard plugin install flows may not preserve extras reliably
- [x] Sendspin source/server starts when plugin initializes
- [x] Sendspin source accepts WAV input from the plugin audio queue worker
- [x] Embedded mode: plugin drives `aiosendspin` directly (Python 3.12+)

#### Test phrase through Sendspin
- [x] Test button sends phrase through the full stack: Piper → cache → embedded `aiosendspin` → connected player(s)

#### Browser player rewrite
- [x] Browser player served at `/player`
- [x] Original root-level `player/` Vite/Preact app scaffolded
- [x] Add `@sendspin/sendspin-js` to the root-level player app
- [x] Configure Vite build output to `custom_plugins/race_voice/player` for plugin release packaging
- [x] Replace template UI with `SendspinPlayer`-based app
- [x] Add compact diagnostics for sync drift, correction mode, reconnects, and player state
- [x] Browser player is served by the RotorHazard plugin; live connection state is owned by the browser player UI, not the RotorHazard settings panel
- [x] Standalone player migration started in `sendspin_player/` with React, Tailwind, shadcn/Radix, and the existing Sendspin SDK contract
- [x] Retire the old root-level `player/` source path from workflows and developer docs
- [x] Remove the old root-level `player/` source directory as part of the player migration

**Success criteria:**
- [x] "Test phrase" plays audibly through the browser player on a remote device
- [x] Back-to-back lap callouts play without an audible stream reset
- [x] Browser player disconnect does not crash the plugin
- [x] Disconnecting and reconnecting the browser player recovers playback automatically

---

## Post-MVP Phases

Post-MVP work starts after the local MVP is race-day usable: full local race
callouts, caching/pre-generation, local Sendspin service playback, and browser
player playback. This section includes upstream-dependent RotorHazard features,
deployment modes, live status polish, and formal latency measurements that are
useful but not required for the first stable release.

---

### Phase 4 — Deferred RH Features, Local Sendspin Service, Cloud, and QR Code

**Goal:** Track RHAPI-dependent callouts outside the MVP, and let Sendspin run
as a packaged local service, in the cloud, or both at the same time. The plugin
remains responsible for RH events, Piper synthesis, WAV caching, queue priority,
and expiry. Sendspin services are output targets.

#### Deferred RH / RHAPI-dependent callouts
- [ ] `Evt.RACE_PILOT_DONE` → "[callsign] finished"
- [ ] Race clock callouts via upstream `Evt.RACE_CLOCK_WARNING`
- [ ] Arm sequence countdown beeps via upstream `Evt.RACE_STAGE_TONE`
- [ ] Last-5-seconds countdown beeps (one `stage.wav` per second for the final 5s) and `buzzer.wav` at race end — mirrors browser behaviour; needs per-second `Evt.RACE_CLOCK_WARNING` thresholds (5, 4, 3, 2, 1) or a dedicated end-of-race countdown mechanism
- [ ] Scheduled race start callouts ("Next race begins in 30 seconds" etc.)
- [ ] Race tied / overtime via upstream `Evt.RACE_TIED` / `Evt.RACE_OVERTIME`
- [ ] Race leader via `Flt.EMIT_PHONETIC_LEADER` (payload: `pilot`, `callsign`; already hookable today — deferred to keep MVP scope small)
- [ ] Audio profile toggles for race clock, arm sequence, race tied/overtime, and race leader callouts
- [ ] Split pass callouts via `Flt.EMIT_PHONETIC_SPLIT` (payload: `pilot_name`, `split_id`, `split_time`, `split_speed`; only relevant on tracks with split sensors)

#### Sendspin output abstraction (superseded by service-only package direction)
- [x] Add a small output protocol/interface with `play(wav_paths, expires_at)` and `stop()`
- [x] Move current `SendSpinServer` usage behind `InternalSendspinOutput`
- [x] Add `RemoteSendspinOutput` that posts WAV jobs to a Sendspin server URL
- [x] Add `FanoutSendspinOutput` that calls all enabled outputs independently
- [x] Use per-target timeouts so cloud latency never delays the local PA
- [x] Log target-specific failures without disabling healthy targets
- [x] Stop button fans out to every enabled target
- [x] Replace internal/external local output abstraction with service-only local HTTP output
- [x] Track detailed package work in `Sendspin Service Package PVA.md`

#### Sendspin server API
Current migration state: `sendspin_service/` owns its own `audio_queue.py`,
`sendspin.py`, and `server.py`. The plugin no longer keeps an internal Sendspin
server fallback. The first service API smoke test has passed for `/health`,
`/v1/play`, and `/v1/stop`.

- [x] Create Sendspin server entrypoint under root-level `sendspin_service/`, separate from RotorHazard plugin initialization
- [x] Server starts `SendSpinServer` and exposes an HTTP ingest API
- [x] `GET /health` returns server status, Sendspin listen port, and connected player count when available
- [x] `POST /v1/play` accepts one or more WAV files and optional expiry metadata
- [x] `POST /v1/stop` stops current playback and clears scheduled audio
- [x] Server appends queued WAVs to the active Sendspin stream, preserving current MVP playback behavior
- [x] Server rejects stale jobs when expiry metadata says playback would start too late

#### Local Sendspin service on Raspberry Pi
- [x] Provide `sendspin.service` systemd unit file with the repo
- [x] Remove plugin setting: Sendspin local mode (Internal server / External server / Disabled)
- [x] Remove internal Sendspin hidden/legacy fallback
- [x] Plugin setting: Local Sendspin URL (default: `http://127.0.0.1:8766`)
- [x] Plugin setting: Local Sendspin API token (optional)
- [x] Plugin setting: Sendspin target dropdown (Local / Cloud / Local + Cloud)
- [ ] Optional plugin health/status display without adding a separate debug-style quick button
- [ ] Add a service API compatibility field and plugin-side handling only when the service API gets a breaking change
- [x] Local service works as a self-contained `.deb` package on amd64
- [x] Play audio check works through the installed local service on amd64
- [x] Local service does not depend on RotorHazard venv, `uv`, `pip`, or user-managed Python
- [x] Document service package install, upgrade, status, logs, remove, and purge

#### Cloud Sendspin target
- [x] Plugin setting: Cloud Sendspin URL
- [x] Plugin setting: Cloud Sendspin API token (optional bearer token)
- [x] Plugin target dropdown supports Cloud-only output
- [x] Plugin target dropdown supports Local + Cloud fan-out
- [x] `SendspinServiceClient` sends WAV jobs to selected targets over HTTP(S)
- [x] Cloud target can run alone for small events without a PA
- [x] Cloud target can run alongside local service for PA + spectator phone feed
- [x] Cloud failures are target-specific log errors and do not prevent attempts to other selected targets
- [x] No change required to the audio queue or TTS pipeline
- [ ] Per-target health/status display in the RotorHazard panel

#### Cloud Docker deployment
- [x] Add Dockerfile for the Sendspin server under `sendspin_service/`
- [x] Add documented environment variables: ingest token, HTTP port, Sendspin listen port, public base URL
- [x] Container exposes the Sendspin player endpoint and HTTP ingest endpoint
- [x] Document reverse-proxy/TLS expectation for public deployments
- [ ] Document a minimal VPS deployment path

#### QR code for spectator/pilot feed
- [ ] Generate QR code from the Sendspin player join URL (cloud target)
- [ ] Display QR code image in plugin settings panel
- [ ] QR code downloadable as PNG (for printing or sharing on screen)
- [ ] Panel note: "Requires internet access from RotorHazard host (hotspot is sufficient)"

#### Reconnect handling
- [ ] Health checks run per enabled target
- [ ] Failed targets retry health checks with exponential backoff: 2 s → 4 s → 8 s → max 60 s
- [ ] Log each reconnect/health attempt and success per target
- [ ] Recovered targets resume receiving new jobs without plugin or RotorHazard restart

**Success criteria:**
- Phone hears race callouts after scanning QR code (cloud path)
- Local Sendspin service restarts independently without restarting RotorHazard; reconnects within 10 seconds
- Local player and cloud player both receive audio simultaneously
- A cloud outage does not delay or break local PA output
- Install from scratch on a Pi works using the local service package path without user-managed `uv`, `pip`, or Python setup

---

### Phase 5 — Wyoming Piper + Polish

**Goal:** Wyoming Piper as a lower-latency TTS option. mDNS player discovery. Cache management UI. Installation guide complete.

#### Wyoming Piper backend
- [ ] Wyoming protocol TCP client: connect to `host:port`, send synthesis request, receive WAV
- [ ] Plugin setting: Wyoming Piper host and port
- [ ] Setting: TTS engine selector (piper-tts / Wyoming Piper)
- [ ] Fallback: if Wyoming unreachable on startup, fall back to in-process piper-tts and show warning
- [ ] Health check: Wyoming service reachable; shown in plugin panel
- [ ] Cache works identically for both backends (same cache key format)

#### mDNS player discovery
- [ ] Discover Sendspin players on local network via mDNS/Zeroconf
- [ ] Show discovered players in plugin panel as selectable targets
- [ ] Manual URL entry remains available and takes precedence
- [ ] mDNS discovery failure is non-fatal (race networks may block mDNS)

#### Cache management UI
- [ ] Show: total cached files, total cache size
- [ ] Button: clear all cached WAVs
- [ ] Button: pre-generate for current heat (manual trigger)
- [ ] Show: list of most recently generated phrases
- [ ] Setting: maximum cache size (MB); evict oldest files when exceeded

#### Installation guide (in this document, final section)
- [ ] Piper: install steps for Raspberry Pi (arm64) and x86_64
- [ ] Piper: download and configure a voice model
- [ ] Wyoming Piper: run as a systemd service
- [ ] Sendspin player: install on NUC / laptop / Pi
- [ ] Local Sendspin service: install/upgrade/remove packaged `.deb` on Pi
- [ ] Cloud Sendspin server: deploy with Docker behind HTTPS
- [ ] RotorHazard browser clients: how to set Voice Volume to 0
- [ ] End-to-end test checklist for a new installation

**Success criteria:**
- Wyoming Piper produces callouts with measurably lower latency than in-process piper-tts for uncached phrases
- mDNS discovers the Sendspin player without manual URL entry on a standard home/club network
- A new user following the installation guide has audio working within 30 minutes
- Cache size stays bounded; old files evicted automatically when limit is reached

---

## Technical Risks

| Risk | Impact | Mitigation |
|---|---|---|
| Sendspin browser playback hiccups on race network | Medium | Validate `/player` on actual event network and speaker hardware before race day |
| Sendspin public preview API changes | Medium | Pin version, document upgrade path |
| Python 3.12 not available in RotorHazard virtualenv | Medium | Move Sendspin dependencies into a separate external Sendspin server venv; keep plugin-side dependencies compatible where possible |
| Cloud target is slow or unreachable | Medium | Fan-out outputs independently; local PA target has its own short timeout and remains healthy |
| piper-tts too slow on Pi for uncached callouts | High | WAV caching now; planned heat-load pre-caching; Wyoming Piper on separate machine as fallback |
| Browser audio not disabled on clients | Duplicate audio | Setup checklist in UI |
| mDNS unreliable on race network | Medium | Manual player URL as primary config |
| Browser WebAudio scheduling drifts after tab or MacBook hiccup | Medium | Use official `@sendspin/sendspin-js` scheduler instead of custom scheduling; expose sync diagnostics |

---

## Data Flow

1. RotorHazard triggers a race event.
2. Plugin event handler builds an audio job (text + priority).
3. Plugin checks cache; on miss, calls piper-tts in-process to generate WAV.
4. Audio job enters the async playback queue.
5. Playback worker sends WAV to the Sendspin source.
6. Sendspin streams audio to connected clients on the local network.
7. Player outputs audio through the connected speaker/mixer.
8. Built-in browser audio remains disabled on normal clients.

---

## References

- [RotorHazard Plugin docs](doc/Plugins.md)
- [RHAPI docs](doc/RHAPI.md)
- [Piper TTS](https://github.com/rhasspy/piper)
- [Wyoming protocol](https://www.home-assistant.io/integrations/wyoming/)
- [Sendspin](https://www.sendspin-audio.com/)
