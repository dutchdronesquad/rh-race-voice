# AGENTS.md

## Project Context

Race Voice is a RotorHazard RHAPI plugin (`custom_plugins/race_voice/`) that forwards race events and state to a standalone Sendspin voice service (`sendspin_service/`) over HTTP. The plugin is a thin event/state adapter with no synthesis of its own; the service owns Piper TTS synthesis, caching, and fan-out playback to Sendspin clients (the browser player and WindowsSpin). See `docs/architecture.md` for the full runtime flow and module map.

Important modules:

Plugin package (`custom_plugins/race_voice/`):

- `__init__.py`: entry point; `initialize()` constructs the event adapter and nothing else.
- `event_adapter.py`: `RaceEventAdapter` — RH event/filter registration, UI registration, and race-state snapshot building.
- `event_output.py`: `EventPublisher` — bounded, gevent-cooperative HTTP delivery to the service's `/v2/*` routes (session, state, clock, events, commands).
- `ui.py`: RotorHazard settings panel, quick buttons, and `/player` blueprint.
- `const.py`: option names, defaults, and the voice model list.
- `services/clock_callouts.py`: race-clock callout phrase planning and reusable pre-cache phrase lists; shared with the standalone service.
- `sendspin_player/`: Vite/React/shadcn source for the browser player; production output is written to `custom_plugins/race_voice/player/`.

Piper synthesis (`piper.py`) and lap-segment planning (`lap_callouts.py`) used to live in this plugin package too, but had no plugin caller (service-only code); they now live under `sendspin_service/synthesis/` and `sendspin_service/race/` respectively.

Service package (`sendspin_service/`), split into subpackages with the entry point at the top:

- `server.py`: process entry point and HTTP app.
- `race/`: `race_ingest.py` (`RaceIngest`, session/admission and the `/v2/*` routes), `race_planner.py` (`PreparationPlanner`, `PlaybackPlanner`, `Destination`, `SendspinPlaybackSink`), `race_protocol.py` (wire-format parsing and the admission/clock state machine), `race_schedule.py` (scheduled-countdown timers), `speech.py` (`SpeechEngine`), `lap_callouts.py`, `cache_commands.py`, `telemetry.py`.
- `synthesis/`: `piper.py` (`PiperSynthesizer`), `synthesis.py`/`synthesis_worker.py` (the bounded synthesis subprocess supervisor and its child protocol).
- `playback/`: `sendspin.py` (`SendSpinServer`, the `aiosendspin` adapter), `audio_queue.py`, `audio_cache.py`, `player.py` (optional static browser-player routes for Docker).

Cross-subpackage imports are absolute (`sendspin_service.playback.audio_queue`, not `..playback.audio_queue`) — ruff (`TID252`) forbids parent-relative imports in this repo. Same-subpackage imports stay relative.

## Runtime Behavior

Keep architecture changes incremental and concrete. For the standalone-service epic, finish and measure one local event-to-audio path before adding cloud routing or personal pilot selections. Reuse existing libraries and phrase logic; avoid generic frameworks or speculative automation. Keep preparation and per-output playback separate where needed for isolation, and use one event-to-audio path.

RotorHazard phonetic filters and server-side race events are used as callout sources. Heavy work must stay off the RotorHazard event/filter thread. The existing executor schedules callout orchestration, but RotorHazard monkey-patches threading with gevent, so that executor alone does not provide native-thread isolation. Keep Piper synthesis and ONNX session construction behind `PiperSynthesizer._run_native()`; keep RH API calls, queue mutations, and status callbacks outside that native boundary.

Lap callouts are intentionally segmented:

- reusable pilot-name segment: `"[name],"`, stored in `precache/pilots/`.
- reusable lap-number segment: `"Lap [n]"`, stored in `precache/laps/`.
- dynamic lap-time phrase: stored in the per-model `tmp/` cache.

Do not clear `precache/` on `HEAT_SET`. A heat change should clear queued audio and `tmp/` only. Operators can use **Prepare pre-cache** to generate race-clock callouts, scheduled-race countdowns, and reusable schedule phrases, pilot-name segments, and lap-number segments. RotorHazard data reset and the **Clear TTS cache** button may clear all model WAV cache content, including `precache/`.

Lap callouts should expire quickly enough to avoid stale race audio. The current lap expiry is intentionally longer than the queue default to handle several pilots crossing close together, but it should remain race-day conservative.

Staging tones depend on upstream `Evt.RACE_STAGE_TONE`. Keep them as direct event integrations for branches that target the RotorHazard version containing that event; do not add a fallback timer that reimplements staging logic in the plugin. Race-clock callouts depend on upstream `Evt.RACE_CLOCK_CALLOUT`. Keep them as direct event integrations for branches that target the RotorHazard version containing that event; do not add a fallback timer that reimplements race-clock countdown logic in the plugin.

## Sendspin Notes

`SendSpinServer.play()` appends normal queued audio to the active stream instead of stopping and restarting playback. Preserve this behavior unless the user explicitly asks for interrupt-style playback.

The Sendspin backend checks expiry again before scheduling audio. Keep this Sendspin-side check when changing queue behavior, because queue delay and stream scheduling delay are separate concerns.

Preserve `play_at` handling for staging tones and other time-sensitive static WAVs. Normal voice callouts should continue to use appended playback unless a change explicitly needs scheduled playback.

Late-joining Sendspin clients should be synced into the active group while playback is still scheduled to continue. Do not remove the periodic late-join sync during idle-tail waiting without replacing it with equivalent behavior.

## Cache Layout

Generated files live under the standalone service's own cache directory (`SENDSPIN_RACE_CACHE_DIR` / `--race-cache-dir`, defaulting to `race-voice-cache` under the service's systemd state directory or Docker volume), not RotorHazard's data directory:

```text
race-voice-cache/
  models/                 downloaded Piper ONNX models
  tts/<model>/            normal cached phrases
  tts/<model>/precache/pilots/
                           reusable pilot-name segments
  tts/<model>/precache/laps/
                           reusable lap-number segments
  tts/<model>/precache/clock/
                           race-clock callout phrases
  tts/<model>/precache/schedule/
                           scheduled-race countdown phrases
  tts/<model>/tmp/        ephemeral lap-time phrases
  tts/<model>/test/       generated test phrases
```

Cache keys are content hashes that must include normalized phrase text and synthesis parameters (plus the model file contents and Piper library version) so changing voice tuning, models, or the Piper dependency does not reuse the wrong WAV.

## Dependency Policy

The hard cutover to v2.0.0 has happened: the plugin entry point (`custom_plugins/race_voice/__init__.py`) always constructs the event adapter and never loads Piper or ONNX inside RH, and `custom_plugins/race_voice/manifest.json` no longer lists `piper-tts` as a plugin dependency. Only the `sendspin-service` optional dependency group in `pyproject.toml` pulls in `piper-tts`, `aiosendspin`, `av`, `numpy`, and `pillow`, for the service's isolated synthesis worker subprocess. Do not add legacy modes, automatic fallbacks, or compatibility adapters for the removed v1 HTTP surface or in-process synthesis path. Rollback means installing the previous release. Missing runtime dependencies should fail through the normal dependency path.

Keep dependencies aligned between `pyproject.toml` and `custom_plugins/race_voice/manifest.json`.

## Development Checks

Python requires 3.12 or newer. Use `uv sync --all-groups` for the Python environment.

Useful checks:

- `uv run ruff check .`
- `uv run ruff format --check .`
- `uv run prek run --all-files`

The browser player source lives in `sendspin_player/`:

- `npm run lint`
- `npm run build`
- `npm run build:plugin`

`npm run build` builds the standalone player for `/`. `npm run build:plugin` builds the RotorHazard plugin player for `/player/`. Both write production files into `custom_plugins/race_voice/player/`. The release workflow uses the plugin build and zips `custom_plugins` as `race_voice.zip`.

## Documentation Style

Write Markdown prose as natural paragraphs without a fixed line-length limit. The Python formatter's 88-character target does not apply to documentation. Preserve intentional line breaks in code blocks, tables and lists.

The README should stay selective: keep it focused on what Race Voice is, what it needs, and how to get started. Move day-to-day operation, settings, cache behavior, and troubleshooting details into files under `docs/`.

Keep user-facing docs aligned with actual race behavior, especially cache cleanup, browser playback, Sendspin port `8927`, and the need to set RotorHazard browser Voice Volume and Tone Volume to `0` when Race Voice handles callouts and race sounds.

## PR Style

Write PR descriptions as a short explanation of the change, not as a raw change log. Start with one or two paragraphs that explain the problem, the chosen direction, and the user-visible result. Use bullet lists only for the parts that are easier to scan as lists, such as notable implementation details, follow-up work, or validation steps.

Avoid PR bodies made entirely of bullet lists. Do not enumerate every touched file or internal refactor unless it changes behavior, deployment, packaging, or the operator workflow. The reader should understand why the branch exists before they see the checklist.

## Changelog Style

Write changelog entries for end users and race operators, not as an internal implementation log.

Use a mixed format:

- Prefer short thematic sections with a few sentences of context.
- Use bullet lists when they make user impact easier to scan.
- Keep bullets meaningful: describe what the user can do, what changed in race-day behavior, or what they need to know before upgrading.
- Avoid long lists of internal modules, helper classes, refactors, or low-level implementation details unless they directly explain a user-visible behavior.
- GitHub Releases can carry the fuller generated change list; `CHANGELOG.md` should remain concise and readable.
