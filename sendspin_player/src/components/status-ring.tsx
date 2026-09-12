import { useEffect, useState } from "react";

export type ConnectionState = "disconnected" | "connecting" | "connected" | "playing" | "reconnecting" | "error";

const arcColor: Record<ConnectionState, string> = {
  disconnected: "stroke-stroke-muted",
  connecting:   "stroke-warning",
  reconnecting: "stroke-warning",
  connected:    "stroke-success",
  playing:      "stroke-primary",
  error:        "stroke-destructive",
};

const dotColor: Record<ConnectionState, string> = {
  disconnected: "bg-stroke-muted",
  connecting:   "bg-warning shadow-[0_0_8px_var(--color-warning)]",
  reconnecting: "bg-warning shadow-[0_0_8px_var(--color-warning)]",
  connected:    "bg-success shadow-[0_0_9px_var(--color-success)]",
  playing:      "",
  error:        "bg-destructive",
};

const svgGlow: Partial<Record<ConnectionState, string>> = {
  connected: "drop-shadow(0 0 4px color-mix(in srgb, var(--color-success) 40%, transparent))",
  error:     "drop-shadow(0 0 4px color-mix(in srgb, var(--color-destructive) 35%, transparent))",
  playing:   "drop-shadow(0 2px 6px rgba(0,0,0,0.45))",
};

const LP_EXIT_MS = 380; // must match lp-exit keyframe duration in index.css

const CIRC    = 2 * Math.PI * 30;
const ARC_LEN = 47;
const GROOVES = [15.2, 16.4, 17.6, 18.8, 20, 21.2, 22.4, 23.6, 24.8, 26, 27.2, 28.4] as const;

export function StatusRing({ state }: { state: ConnectionState }) {
  const spinning = state === "connecting" || state === "reconnecting";
  const pulsing  = state === "playing";

  const [lpVisible, setLpVisible] = useState(pulsing);
  const [wasPulsing, setWasPulsing] = useState(pulsing);

  // Show the LP immediately when playback starts, including during an exit.
  // This guarded update adjusts only this component's state before committing.
  // https://react.dev/reference/react/useState#storing-information-from-previous-renders
  if (pulsing !== wasPulsing) {
    setWasPulsing(pulsing);
    if (pulsing) setLpVisible(true);
  }

  const lpExiting = lpVisible && !pulsing;

  useEffect(() => {
    if (!lpExiting) return;

    // Keep the LP mounted until its exit finishes; cancel if playback resumes.
    const t = setTimeout(() => setLpVisible(false), LP_EXIT_MS);
    return () => clearTimeout(t);
  }, [lpExiting]);

  const showRing = !lpVisible;
  const filter   = svgGlow[lpVisible ? "playing" : state];

  return (
    <div className="relative size-[76px]">

      <svg
        viewBox="0 0 72 72"
        overflow="visible"
        className="relative size-full"
        style={filter ? { filter } : undefined}
        aria-hidden="true"
      >
        <defs>
          <radialGradient id="label-shine" cx="38%" cy="34%" r="70%">
            <stop offset="0%"   stopColor="white" stopOpacity="0.18" />
            <stop offset="100%" stopColor="black" stopOpacity="0.14" />
          </radialGradient>
        </defs>

        {/* ── Playing: LP vinyl record ─────────────────────────────────────── */}
        {lpVisible && (
          <g
            className={lpExiting ? undefined : "animate-[lp-appear_0.45s_ease-out_both]"}
            style={{
              transformOrigin: "36px 36px",
              ...(lpExiting && { animation: `lp-exit ${LP_EXIT_MS}ms ease-in both` }),
            }}
          >
            <g className="animate-[spin_6s_linear_infinite]" style={{ transformOrigin: "36px 36px" }}>
              <circle cx="36" cy="36" r="30" fill="rgb(10,9,14)" />
              {GROOVES.map((r, i) => (
                <circle key={r} cx="36" cy="36" r={r} fill="none"
                  stroke="rgba(255,255,255,0.07)" strokeWidth={i % 2 === 0 ? "0.6" : "0.38"} />
              ))}
              <circle cx="36" cy="36" r="29"   fill="none" stroke="rgba(255,255,255,0.04)" strokeWidth="0.4" />
              <circle cx="36" cy="36" r="29.6" fill="none" stroke="rgba(255,255,255,0.28)" strokeWidth="0.7" />
              <circle cx="36" cy="36" r="30"   fill="none" stroke="rgba(0,0,0,0.6)"        strokeWidth="0.5" />
              <circle cx="36" cy="36" r="14.3" fill="rgb(8,7,11)" />
              <circle cx="36" cy="36" r="14"   fill="none" stroke="rgba(255,255,255,0.20)" strokeWidth="0.5" />
              <circle cx="36" cy="36" r="13.5" fill="var(--color-primary)" />
              <circle cx="36" cy="36" r="11"   fill="none" stroke="rgba(0,0,0,0.15)" strokeWidth="0.4"  />
              <circle cx="36" cy="36" r="8"    fill="none" stroke="rgba(0,0,0,0.10)" strokeWidth="0.35" />
              <circle cx="36" cy="36" r="5"    fill="none" stroke="rgba(0,0,0,0.07)" strokeWidth="0.3"  />
              <line x1="36" y1="22.8" x2="36" y2="33.6"
                stroke="rgba(255,255,255,0.50)" strokeWidth="1.6" strokeLinecap="round" />
              <path d="M 33.2 22.4 A 14 14 0 0 1 38.8 22.4"
                fill="none" stroke="rgba(255,255,255,0.55)" strokeWidth="1.0" strokeLinecap="round" />
              <circle cx="36" cy="7.6" r="2.2" fill="rgba(255,255,255,0.55)" />
              <circle cx="36" cy="36" r="13.5" fill="url(#label-shine)" />
              <circle cx="36" cy="36" r="2.2"  fill="rgb(5,5,8)" />
            </g>
          </g>
        )}

        {/* ── Other states: normal arc ring ────────────────────────────────── */}
        {showRing && (
          <g key="ring" className="animate-[ring-appear_0.35s_ease-out_both]">
            <circle cx="36" cy="36" r="30" className="fill-none stroke-muted [stroke-width:4]" />
            {spinning ? (
              <g
                className={`animate-[spin_1.5s_linear_infinite] ${arcColor[state]}`}
                style={{ transformOrigin: "36px 36px" }}
              >
                <circle
                  cx="36" cy="36" r="30"
                  className="fill-none [stroke-width:4] [stroke-linecap:round]"
                  style={{ strokeDasharray: `${ARC_LEN} ${CIRC - ARC_LEN}` }}
                />
              </g>
            ) : state === "connected" ? (
              <circle
                key="connected"
                cx="36" cy="36" r="30"
                className={`fill-none [stroke-width:4] [stroke-linecap:round] [transform:rotate(-90deg)] [transform-origin:50%_50%] [animation:ring-fill_0.65s_ease-out_both,ring-ready_3s_ease-in-out_0.65s_infinite] ${arcColor[state]}`}
              />
            ) : state === "error" ? (
              <circle
                key="error"
                cx="36" cy="36" r="30"
                className={`fill-none [stroke-width:4] [stroke-linecap:round] [transform:rotate(-90deg)] [transform-origin:50%_50%] [animation:ring-error_0.4s_ease-in-out_4_forwards] ${arcColor[state]}`}
                style={{ strokeDasharray: CIRC }}
              />
            ) : (
              <circle
                key={state}
                cx="36" cy="36" r="30"
                className={`fill-none [stroke-width:4] [stroke-linecap:round] transition-[stroke] duration-300 [transform:rotate(-90deg)] [transform-origin:50%_50%] ${arcColor[state]}`}
                style={{ strokeDasharray: CIRC }}
              />
            )}
          </g>
        )}
      </svg>

      {/* Status dot — all non-playing states */}
      {showRing && (
        <div className="absolute inset-0 flex items-center justify-center">
          <span
            key={state}
            className={`size-[11px] rounded-full animate-[dot-pop_0.3s_ease-out_both] ${dotColor[state]}`}
          />
        </div>
      )}
    </div>
  );
}
