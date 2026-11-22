import assert from "node:assert/strict";
import test from "node:test";
import type { SendspinPlayer } from "@sendspin/sendspin-js";
import { PlayerDiagnosticCapture, bufferedRunwayMs } from "../src/lib/player-diagnostics.ts";

function player(withScheduler = true): SendspinPlayer {
  return {
    isConnected: true, isPlaying: true,
    pairingToken: "never-export-this", clientId: "private-stable-id",
    syncInfo: {
      syncErrorMs: 4, outputLatencyMs: 20, playbackRate: 1,
      correctionMethod: "resync", correctionMode: "sync",
      resyncCount: 3, samplesAdjusted: 2,
    },
    timeSyncInfo: { synced: true, offset: 5, error: 2 },
    getCurrentServerTimeUs: () => 123456,
    ...(withScheduler ? { scheduler: {
      measureBufferedPlaybackRunwaySec: () => 0.15,
      getAudioContext: () => ({ getOutputTimestamp: () => ({ contextTime: 10, performanceTime: 1000 }) }),
    } } : {}),
  } as unknown as SendspinPlayer;
}

test("recording is opt-in, bounded, chronological and excludes credentials", () => {
  const capture = new PlayerDiagnosticCapture();
  capture.sample(player());
  assert.equal(capture.snapshot("6.0.0", "test-browser").samples.length, 0);
  capture.start();
  capture.event("connected");
  for (let index = 0; index < 1250; index++) capture.sample(player());
  for (let index = 0; index < 210; index++) capture.event("operator_marker");
  const report = capture.snapshot("6.0.0", "test-browser");
  assert.equal(report.samples.length, 1200);
  assert.equal(report.events.length, 200);
  assert.equal(report.overwritten_samples, 50);
  assert.equal(report.overwritten_events, 11);
  assert.equal(report.samples[0].buffered_runway_ms, 150);
  assert.equal(report.samples[0].resync_count, 3);
  assert.equal(report.samples[0].render_context_s, 10);
  assert.ok(Number(report.samples[0].t_ms) <= Number(report.samples.at(-1)?.t_ms));
  assert.ok(!JSON.stringify(report).includes("never-export-this"));
  assert.ok(!JSON.stringify(report).includes("private-stable-id"));
  capture.stop();
  capture.sample(player());
  assert.equal(capture.snapshot("6.0.0", "test-browser").overwritten_samples, 50);
});

test("unsupported buffer/render counters are unavailable rather than zero", () => {
  const capture = new PlayerDiagnosticCapture();
  capture.start();
  capture.sample(player(false));
  const sample = capture.snapshot("6.0.0", "test-browser").samples[0];
  assert.equal(sample.buffered_runway_ms, null);
  assert.equal(sample.render_context_s, null);
  assert.ok(capture.snapshot("6.0.0", "test-browser").unavailable.includes("authoritative_underrun_count"));
  const broken = player();
  Reflect.set(broken, "scheduler", { measureBufferedPlaybackRunwaySec: () => { throw new Error("unavailable"); } });
  assert.equal(bufferedRunwayMs(broken), null);
});
