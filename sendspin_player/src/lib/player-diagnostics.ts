import type { SendspinPlayer } from "@sendspin/sendspin-js";

const MAX_SAMPLES = 1200;
const MAX_EVENTS = 200;
type RecordData = Record<string, string | number | boolean | null>;
type EventKind = "connected" | "disconnected" | "reconnecting" | "reconnected" | "error" | "operator_marker";

function finite(value: unknown): number | null {
  return typeof value === "number" && Number.isFinite(value) ? value : null;
}

function scheduler(player: SendspinPlayer): object | null {
  const value: unknown = Reflect.get(player, "scheduler");
  return typeof value === "object" && value !== null ? value : null;
}

export function bufferedRunwayMs(player: SendspinPlayer): number | null {
  // The SDK exposes this method on its scheduler, but not on SendspinPlayer.
  // Treat it as an optional capability, like the existing visualizer tap.
  try {
    const target = scheduler(player);
    if (target === null) return null;
    const measure: unknown = Reflect.get(target, "measureBufferedPlaybackRunwaySec");
    if (typeof measure !== "function") return null;
    const seconds = finite(Reflect.apply(measure, target, []));
    return seconds === null ? null : seconds * 1000;
  } catch { return null; }
}

function renderTimestamp(player: SendspinPlayer): RecordData {
  const missing = { render_context_s: null, render_performance_ms: null };
  try {
    const target = scheduler(player);
    if (target === null) return missing;
    const getContext: unknown = Reflect.get(target, "getAudioContext");
    if (typeof getContext !== "function") return missing;
    const context: unknown = Reflect.apply(getContext, target, []);
    if (typeof context !== "object" || context === null) return missing;
    const getTimestamp: unknown = Reflect.get(context, "getOutputTimestamp");
    if (typeof getTimestamp !== "function") return missing;
    const stamp: unknown = Reflect.apply(getTimestamp, context, []);
    if (typeof stamp !== "object" || stamp === null) return missing;
    return {
      render_context_s: finite(Reflect.get(stamp, "contextTime")),
      render_performance_ms: finite(Reflect.get(stamp, "performanceTime")),
    };
  } catch { return missing; }
}

function ordered(values: RecordData[], count: number, limit: number): RecordData[] {
  const offset = count >= limit ? count % limit : 0;
  return [...values.slice(offset), ...values.slice(0, offset)];
}

export class PlayerDiagnosticCapture {
  active = false;
  private samples: RecordData[] = [];
  private events: RecordData[] = [];
  private sampleCount = 0;
  private eventCount = 0;
  private captureId = "";
  private startedUtc = "";
  private generation = 0;

  start(): void {
    this.samples = [];
    this.events = [];
    this.sampleCount = this.eventCount = this.generation = 0;
    this.captureId = [...crypto.getRandomValues(new Uint8Array(16))]
      .map(value => value.toString(16).padStart(2, "0")).join("");
    this.startedUtc = new Date().toISOString();
    this.active = true;
  }

  stop(): void { this.active = false; }

  event(kind: EventKind): void {
    if (!this.active) return;
    if (kind === "connected" || kind === "reconnected") this.generation += 1;
    this.events[this.eventCount++ % MAX_EVENTS] = {
      kind, t_ms: performance.now(), utc_ms: Date.now(), connection: this.generation,
    };
  }

  sample(player: SendspinPlayer | null): void {
    if (!this.active) return;
    const sample: RecordData = {
      t_ms: performance.now(), utc_ms: Date.now(), connection: this.generation,
      connected: player?.isConnected ?? false,
    };
    try {
      if (player !== null) {
        const sync = player.syncInfo;
        const clock = player.timeSyncInfo;
        Object.assign(sample, {
          playing: player.isPlaying,
          time_synced: clock.synced,
          server_time_us: clock.synced ? finite(player.getCurrentServerTimeUs()) : null,
          clock_offset_ms: finite(clock.offset), clock_error_ms: finite(clock.error),
          sync_error_ms: finite(sync.syncErrorMs),
          output_latency_ms: finite(sync.outputLatencyMs),
          playback_rate: finite(sync.playbackRate),
          correction: sync.correctionMethod, correction_mode: sync.correctionMode,
          resync_count: finite(sync.resyncCount),
          samples_adjusted: finite(sync.samplesAdjusted),
          buffered_runway_ms: bufferedRunwayMs(player),
          ...renderTimestamp(player),
        });
      }
    } catch { sample.sampling_error = true; }
    this.samples[this.sampleCount++ % MAX_SAMPLES] = sample;
  }

  snapshot(sdkVersion: string, userAgent: string) {
    return {
      schema: "race-player-diagnostics/1", capture_id: this.captureId,
      started_utc: this.startedUtc, exported_utc: new Date().toISOString(),
      clock_domain: "browser performance.now; server_time_us is SDK-mapped with clock_error_ms",
      performance_time_origin_ms: performance.timeOrigin,
      sdk_version: sdkVersion, user_agent: userAgent.slice(0, 512),
      limits: { samples: MAX_SAMPLES, events: MAX_EVENTS, sampling_interval_ms: 500 },
      overwritten_samples: Math.max(0, this.sampleCount - MAX_SAMPLES),
      overwritten_events: Math.max(0, this.eventCount - MAX_EVENTS),
      unavailable: ["hardware_audible_event_time", "authoritative_underrun_count"],
      interpretation: "An empty runway or a resync is an observation, not proof of an audible glitch. Missing SDK/browser capabilities are null.",
      samples: ordered(this.samples, this.sampleCount, MAX_SAMPLES),
      events: ordered(this.events, this.eventCount, MAX_EVENTS),
    };
  }
}
