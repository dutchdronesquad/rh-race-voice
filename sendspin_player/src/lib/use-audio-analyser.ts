import { useEffect, useRef, type RefObject } from "react";
import type { SendspinPlayer } from "@sendspin/sendspin-js";

/**
 * Taps into the SendspinPlayer's internal AudioContext via AudioScheduler.getAudioContext()
 * and connects an AnalyserNode to the gain node for frequency visualization.
 * Degrades gracefully if SDK internals change.
 */
export function useAudioAnalyser(
  playerRef: RefObject<SendspinPlayer | null>,
  playing: boolean,
): RefObject<AnalyserNode | null> {
  const analyserRef = useRef<AnalyserNode | null>(null);

  useEffect(() => {
    const prev = analyserRef.current;
    if (prev) {
      try { prev.disconnect(); } catch { /* ignore */ }
      analyserRef.current = null;
    }

    const player = playerRef.current;
    if (!player || !playing) return;

    try {
      // SDK internals are private: validate their runtime shape before using them.
      const scheduler: unknown = Reflect.get(player, "scheduler");
      if (
        typeof scheduler !== "object" || scheduler === null ||
        !("getAudioContext" in scheduler) || typeof scheduler.getAudioContext !== "function" ||
        !("gainNode" in scheduler)
      ) return;

      const audioCtx: unknown = scheduler.getAudioContext();
      const gainNode = scheduler.gainNode;
      if (!(audioCtx instanceof AudioContext) || !(gainNode instanceof AudioNode)) return;

      const analyser = audioCtx.createAnalyser();
      analyser.fftSize = 128;               // 64 frequency bins
      analyser.smoothingTimeConstant = 0.8;
      gainNode.connect(analyser);           // additive tap — doesn't break existing routing
      analyserRef.current = analyser;

      return () => {
        try { gainNode.disconnect(analyser); } catch { /* ignore */ }
        try { analyser.disconnect(); } catch { /* ignore */ }
        analyserRef.current = null;
      };
    } catch {
      // SDK internals unavailable — visualizer silently disabled
    }
  }, [playerRef, playing]);

  return analyserRef;
}
