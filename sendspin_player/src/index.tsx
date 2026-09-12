import { SendspinPlayer } from "@sendspin/sendspin-js";
import type { CorrectionMode, GroupUpdatePayload, ServerStatePayload, StreamFormat } from "@sendspin/sendspin-js";
import {
  ChevronRightIcon,
  FlaskConicalIcon,
  PlayIcon,
  RotateCcwIcon,
  Share2Icon,
  Volume2Icon,
  VolumeXIcon,
} from "lucide-react";
import { useEffect, useMemo, useRef, useState } from "react";
import { Button } from "@/components/ui/button";
import { Collapsible, CollapsibleContent, CollapsibleTrigger } from "@/components/ui/collapsible";
import { Dialog, DialogContent, DialogHeader, DialogTitle } from "@/components/ui/dialog";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select";
import { Slider } from "@/components/ui/slider";
import { Tooltip, TooltipContent, TooltipTrigger } from "@/components/ui/tooltip";
import { VisualizerCanvas } from "@/components/visualizer-canvas";
import { QrCodeSvg } from "@/components/qr-code-svg";
import { StatusRing } from "@/components/status-ring";
import type { ConnectionState } from "@/components/status-ring";
import { useAudioAnalyser } from "@/lib/use-audio-analyser";
import "./index.css";

// ── Constants ────────────────────────────────────────────────────────────────

const STORE_SERVER_URL = "raceVoice.player.serverUrl";
const STORE_VOLUME = "raceVoice.player.volume";
const STORE_MUTED = "raceVoice.player.muted";
const STORE_MODE = "raceVoice.player.correctionMode";
const SENDSPIN_DEMO_URL = "https://sendspin-demo.openhomefoundation.org";
const DEFAULT_VOLUME = 80;
const DEFAULT_CORRECTION_MODE: CorrectionMode = "sync";
const CODECS = ["pcm"] as const;
const CORRECTION_DESCRIPTIONS: Record<CorrectionMode, string> = {
  sync:            "Tight sample-level correction — best for local network.",
  quality:         "Gradual rate adjustment for smooth playback — tolerates drift.",
  "quality-local": "Uses device clock as reference — for offline or unreliable connections.",
};

const CORRECTION_THRESHOLDS = {
  sync: {
    deadbandBelowMs: 2,
    rate1AboveMs: 12,
    rate2AboveMs: 38,
    samplesBelowMs: 8,
    resyncAboveMs: 220,
  },
  quality: {
    deadbandBelowMs: 2,
    samplesBelowMs: 24,
    resyncAboveMs: 55,
  },
};
const RECONNECT_CONFIG = {
  baseDelayMs: 1000,
  maxDelayMs: 15000,
  maxAttempts: Infinity,
};

// ── Types ────────────────────────────────────────────────────────────────────

type PlayerSnapshot = {
  isConnected: boolean;
  isPlaying: boolean;
  volume: number;
  muted: boolean;
  playerState: string;
  format: StreamFormat | null;
  syncErrorMs: number | null;
  outputLatencyMs: number | null;
  correctionMethod: string;
  playbackRate: number | null;
  timeSynced: boolean;
};

type LogEntry = {
  id: number;
  kind: "info" | "warn" | "error" | "play";
  message: string;
};

type PlayerEventState = {
  isPlaying: boolean;
  volume: number;
  muted: boolean;
  playerState: string;
  serverState: ServerStatePayload;
  groupState: GroupUpdatePayload;
};

type DetailLogState = {
  correctionMethod: string;
  formatKey: string | null;
  groupPlayback: string | null;
  latencyBucket: number | null;
  syncBucket: number | null;
  timeSynced: boolean;
  trackLabel: string | null;
};

// ── Helpers ──────────────────────────────────────────────────────────────────

function defaultBaseUrl(): string {
  if (window.location.protocol === "https:") return window.location.origin;
  return `http://${window.location.hostname}:8927`;
}

function normalizeBaseUrl(input: string): string {
  const trimmed = input.trim() || defaultBaseUrl();
  const withScheme = /^[a-z][a-z0-9+.-]*:\/\//i.test(trimmed)
    ? trimmed
    : `${window.location.protocol === "https:" ? "https" : "http"}://${trimmed}`;
  const url = new URL(withScheme, window.location.href);
  if (url.protocol === "ws:") url.protocol = "http:";
  if (url.protocol === "wss:") url.protocol = "https:";
  if (url.protocol !== "http:" && url.protocol !== "https:") {
    throw new Error(`Unsupported protocol: ${url.protocol}`);
  }
  url.pathname = url.pathname.replace(/\/sendspin\/?$/i, "");
  if (url.pathname === "") url.pathname = "/";
  url.search = "";
  url.hash = "";
  return url.toString().replace(/\/$/, "");
}

function initialVolume(): number {
  const storedStr = window.localStorage.getItem(STORE_VOLUME);
  if (storedStr === null) return DEFAULT_VOLUME;
  const stored = Number(storedStr);
  return Number.isFinite(stored) && stored >= 0 && stored <= 100 ? stored : DEFAULT_VOLUME;
}

function initialMuted(): boolean {
  return window.localStorage.getItem(STORE_MUTED) === "true";
}

function initialCorrectionMode(): CorrectionMode {
  const value = window.localStorage.getItem(STORE_MODE);
  return value === "sync" || value === "quality" || value === "quality-local" ? value : DEFAULT_CORRECTION_MODE;
}

function formatNumber(value: number | null, suffix = ""): string {
  if (value === null || !Number.isFinite(value)) return "-";
  return `${value.toFixed(1)}${suffix}`;
}

function formatStream(format: StreamFormat | null): string {
  if (!format) return "-";
  const bits = format.bit_depth ? `${format.bit_depth}bit` : "audio";
  return `${format.codec} · ${format.sample_rate} Hz · ${format.channels}ch · ${bits}`;
}

function formatMetadata(serverState: ServerStatePayload): string | null {
  const metadata = serverState.metadata;
  if (!metadata) return null;
  const title = metadata.title?.trim();
  const artist = metadata.artist?.trim();
  if (title && artist) return `${title} - ${artist}`;
  return title || artist || null;
}

function bucket(value: number | null, size: number): number | null {
  if (value === null || !Number.isFinite(value)) return null;
  return Math.round(value / size) * size;
}

function playerPageUrl(): string {
  const url = new URL(import.meta.env.BASE_URL, window.location.origin);
  url.search = "";
  url.hash = "";
  if (url.pathname !== "/") {
    url.pathname = url.pathname.replace(/\/+$/, "");
  }
  return url.toString();
}

// ── Metric cell ──────────────────────────────────────────────────────────────

function Metric({ label, value }: { label: string; value: string }) {
  return (
    <div className="min-w-0 border-r border-b border-border px-4 py-[0.7rem] odd:[&:nth-last-child(-n+2)]:border-b-0 even:border-r-0 even:[&:nth-last-child(-n+2)]:border-b-0">
      <span className="block text-[0.68rem] tracking-[0.02em] text-muted-foreground">{label}</span>
      <strong className="mt-1 block overflow-hidden text-ellipsis whitespace-nowrap text-[0.76rem] font-semibold text-foreground">
        {value}
      </strong>
    </div>
  );
}

// ── App ──────────────────────────────────────────────────────────────────────

export function App() {
  const [baseUrl, setBaseUrl] = useState(() => window.localStorage.getItem(STORE_SERVER_URL) || defaultBaseUrl());
  const [volume, setVolume] = useState(initialVolume);
  const [muted, setMuted] = useState(initialMuted);
  const [correctionMode, setCorrectionMode] = useState<CorrectionMode>(initialCorrectionMode);
  const [serverOpen, setServerOpen] = useState(true);
  const [diagnosticsOpen, setDiagnosticsOpen] = useState(false);
  const [shareOpen, setShareOpen] = useState(false);
  const [state, setState] = useState<ConnectionState>("disconnected");
  const [logs, setLogs] = useState<LogEntry[]>([]);
  const [snapshot, setSnapshot] = useState<PlayerSnapshot>({
    isConnected: false,
    isPlaying: false,
    volume,
    muted,
    playerState: "synchronized",
    format: null,
    syncErrorMs: null,
    outputLatencyMs: null,
    correctionMethod: "none",
    playbackRate: null,
    timeSynced: false,
  });

  const playerRef = useRef<SendspinPlayer | null>(null);
  const logRef = useRef<HTMLElement>(null);
  const logIdRef = useRef(0);
  const hasConnectedRef = useRef(false);
  const lastPlayingRef = useRef(false);
  const detailLogRef = useRef<DetailLogState>({
    correctionMethod: "none",
    formatKey: null,
    groupPlayback: null,
    latencyBucket: null,
    syncBucket: null,
    timeSynced: false,
    trackLabel: null,
  });

  const shareUrl = useMemo(() => playerPageUrl(), []);
  const analyserRef = useAudioAnalyser(playerRef, state === "playing");

  function addLog(message: string, kind: LogEntry["kind"] = "info") {
    setLogs((current) => [...current, { id: ++logIdRef.current, kind, message }].slice(-80));
  }

  function resetDetailLogs() {
    detailLogRef.current = {
      correctionMethod: "none",
      formatKey: null,
      groupPlayback: null,
      latencyBucket: null,
      syncBucket: null,
      timeSynced: false,
      trackLabel: null,
    };
  }

  function readSnapshot(player: SendspinPlayer): PlayerSnapshot {
    const syncInfo = player.syncInfo;
    return {
      isConnected: player.isConnected,
      isPlaying: player.isPlaying,
      volume: player.volume,
      muted: player.muted,
      playerState: player.playerState,
      format: player.currentFormat,
      syncErrorMs: syncInfo.syncErrorMs,
      outputLatencyMs: syncInfo.outputLatencyMs,
      correctionMethod: syncInfo.correctionMethod,
      playbackRate: syncInfo.playbackRate,
      timeSynced: player.timeSyncInfo.synced,
    };
  }

  function stateFromSnapshot(nextSnapshot: PlayerSnapshot): ConnectionState {
    if (!nextSnapshot.isConnected) {
      return hasConnectedRef.current ? "reconnecting" : "connecting";
    }
    return nextSnapshot.isPlaying ? "playing" : "connected";
  }

  function logPlaybackDetails(player: SendspinPlayer, nextSnapshot: PlayerSnapshot, nextState: PlayerEventState) {
    const detail = detailLogRef.current;
    const groupPlayback = nextState.groupState.playback_state || null;
    const trackLabel = formatMetadata(nextState.serverState);
    const formatKey = nextSnapshot.format ? formatStream(nextSnapshot.format) : null;
    const syncBucket = bucket(nextSnapshot.syncErrorMs, 5);
    const latencyBucket = bucket(nextSnapshot.outputLatencyMs, 10);

    if (groupPlayback && groupPlayback !== detail.groupPlayback) {
      addLog(`Group playback: ${groupPlayback}`, groupPlayback === "playing" ? "play" : "info");
      detail.groupPlayback = groupPlayback;
    }
    if (trackLabel && trackLabel !== detail.trackLabel) {
      addLog(`Now playing: ${trackLabel}`, "play");
      detail.trackLabel = trackLabel;
    }
    if (formatKey && formatKey !== detail.formatKey) {
      addLog(`Stream format: ${formatKey}`);
      detail.formatKey = formatKey;
    }
    if (nextSnapshot.timeSynced !== detail.timeSynced) {
      detail.timeSynced = nextSnapshot.timeSynced;
      if (nextSnapshot.timeSynced) {
        const timeSync = player.timeSyncInfo;
        addLog(`Clock synced: offset ${timeSync.offset} ms, error ±${timeSync.error} ms`);
      } else {
        addLog("Clock sync lost", "warn");
      }
    }
    if (nextSnapshot.isPlaying && nextSnapshot.correctionMethod !== detail.correctionMethod) {
      detail.correctionMethod = nextSnapshot.correctionMethod;
      addLog(
        `Correction: ${nextSnapshot.correctionMethod} · error ${formatNumber(nextSnapshot.syncErrorMs, " ms")}`,
        nextSnapshot.correctionMethod === "resync" ? "warn" : "info",
      );
    }
    if (nextSnapshot.isPlaying && syncBucket !== null && syncBucket !== detail.syncBucket) {
      detail.syncBucket = syncBucket;
      if (Math.abs(syncBucket) >= 5) addLog(`Sync error: ${formatNumber(nextSnapshot.syncErrorMs, " ms")}`);
    }
    if (nextSnapshot.isPlaying && latencyBucket !== null && latencyBucket !== detail.latencyBucket) {
      detail.latencyBucket = latencyBucket;
      addLog(`Output latency: ${formatNumber(nextSnapshot.outputLatencyMs, " ms")}`);
    }
  }

  async function connect() {
    if (playerRef.current) return;

    let normalizedUrl: string;
    try {
      normalizedUrl = normalizeBaseUrl(baseUrl);
    } catch (err) {
      const message = err instanceof Error ? err.message : "Invalid server URL";
      setState("error");
      addLog(message, "error");
      return;
    }

    setBaseUrl(normalizedUrl);
    window.localStorage.setItem(STORE_SERVER_URL, normalizedUrl);
    setState("connecting");
    hasConnectedRef.current = false;
    resetDetailLogs();
    addLog(`Connecting to ${normalizedUrl}`);

    const player = new SendspinPlayer({
      baseUrl: normalizedUrl,
      clientName: "Race Voice Browser Player",
      codecs: [...CODECS],
      correctionMode,
      correctionThresholds: CORRECTION_THRESHOLDS,
      reconnect: {
        ...RECONNECT_CONFIG,
        onReconnecting: (attempt) => {
          setState("reconnecting");
          addLog(`Reconnecting (${attempt})`, "warn");
        },
        onReconnected: () => {
          hasConnectedRef.current = true;
          resetDetailLogs();
          const nextSnapshot = readSnapshot(player);
          setSnapshot(nextSnapshot);
          setState(stateFromSnapshot(nextSnapshot));
          addLog("Reconnected");
        },
        onExhausted: () => {
          setState("error");
          addLog("Reconnect attempts exhausted", "error");
        },
      },
      onStateChange: (nextState) => {
        if (playerRef.current !== player) return;
        const nextSnapshot = readSnapshot(player);
        setSnapshot(nextSnapshot);
        setState(stateFromSnapshot(nextSnapshot));
        logPlaybackDetails(player, nextSnapshot, nextState);
        if (nextState.isPlaying !== lastPlayingRef.current) {
          lastPlayingRef.current = nextState.isPlaying;
          addLog(
            nextState.isPlaying
              ? `Playback started${nextSnapshot.format ? ` · ${formatStream(nextSnapshot.format)}` : ""}`
              : "Playback finished",
            nextState.isPlaying ? "play" : "info",
          );
        }
      },
    });

    playerRef.current = player;
    player.setVolume(volume);
    player.setMuted(muted);

    try {
      await player.unlock();
      await player.connect();
      hasConnectedRef.current = true;
      const nextSnapshot = readSnapshot(player);
      setSnapshot(nextSnapshot);
      setState(stateFromSnapshot(nextSnapshot));
      setServerOpen(false);
      addLog("Connected");
    } catch (err) {
      player.disconnect("shutdown");
      playerRef.current = null;
      hasConnectedRef.current = false;
      const message = err instanceof Error ? err.message : "Connection failed";
      setState("error");
      setServerOpen(true);
      addLog(message, "error");
    }
  }

  function disconnect() {
    const player = playerRef.current;
    playerRef.current = null;
    hasConnectedRef.current = false;
    lastPlayingRef.current = false;
    resetDetailLogs();
    if (player) player.disconnect("user_request");
    setState("disconnected");
    setServerOpen(true);
    setSnapshot((current) => ({ ...current, isConnected: false, isPlaying: false, format: null, timeSynced: false }));
    addLog("Disconnected", "warn");
  }

  function resetServerUrl() {
    const nextUrl = defaultBaseUrl();
    setBaseUrl(nextUrl);
    window.localStorage.removeItem(STORE_SERVER_URL);
    addLog(`Server URL reset to ${nextUrl}`);
  }

  function useDemoServerUrl() {
    setBaseUrl(SENDSPIN_DEMO_URL);
    window.localStorage.setItem(STORE_SERVER_URL, SENDSPIN_DEMO_URL);
    addLog("Server URL set to Sendspin demo");
  }

  function updateVolume(value: number) {
    setVolume(value);
    window.localStorage.setItem(STORE_VOLUME, String(value));
    playerRef.current?.setVolume(value);
  }

  function updateMuted(value: boolean) {
    setMuted(value);
    window.localStorage.setItem(STORE_MUTED, String(value));
    playerRef.current?.setMuted(value);
  }

  function updateCorrectionMode(value: CorrectionMode) {
    setCorrectionMode(value);
    window.localStorage.setItem(STORE_MODE, value);
    playerRef.current?.setCorrectionMode(value);
    addLog(`Correction mode: ${value}`);
  }

  useEffect(() => {
    const interval = window.setInterval(() => {
      const player = playerRef.current;
      if (player) {
        const nextSnapshot = readSnapshot(player);
        setSnapshot(nextSnapshot);
        if (!nextSnapshot.isConnected) setState(stateFromSnapshot(nextSnapshot));

      }
    }, 500);
    return () => window.clearInterval(interval);
  }, []);

  useEffect(
    () => () => {
      playerRef.current?.disconnect("shutdown");
    },
    [],
  );

  useEffect(() => {
    if (logRef.current) logRef.current.scrollTop = logRef.current.scrollHeight;
  }, [logs]);

  const connected =
    state === "connected" || state === "playing" || state === "connecting" || state === "reconnecting";

  const statusText =
    state === "playing"
      ? "Playing"
      : state === "connected"
        ? "Ready"
        : state === "reconnecting"
          ? "Reconnecting..."
          : state === "connecting"
            ? "Connecting..."
            : state === "error"
              ? "Connection error"
              : "Disconnected";

  return (
    <main className="relative flex min-h-dvh items-start justify-center px-4 py-6 sm:items-center">

      {/* Backdrop blobs */}
      <div className="fixed inset-0 z-0 overflow-hidden pointer-events-none" aria-hidden="true">
        <div
          className="absolute rounded-full blur-[120px] animate-[blob-a_14s_ease-in-out_infinite_alternate]"
          style={{
            width: "80vw", height: "72vw",
            top: "-24vw", left: "-14vw",
            background: "radial-gradient(circle, var(--color-primary), transparent 68%)",
            opacity: 0.38,
          }}
        />
        <div
          className="absolute rounded-full blur-[130px] animate-[blob-b_18s_ease-in-out_infinite_alternate]"
          style={{
            width: "78vw", height: "70vw",
            bottom: "-22vw", right: "-16vw",
            background: "radial-gradient(circle, var(--color-success), transparent 68%)",
            opacity: 0.42,
          }}
        />
        <div
          className="absolute rounded-full blur-[100px] animate-[blob-a_22s_ease-in-out_infinite_alternate-reverse]"
          style={{
            width: "45vw", height: "40vw",
            top: "-10vw", right: "-10vw",
            background: "radial-gradient(circle, var(--color-success), transparent 70%)",
            opacity: 0.22,
          }}
        />
      </div>

      {/* Vignette */}
      <div
        className="fixed inset-0 z-0 pointer-events-none"
        aria-hidden="true"
        style={{
          background: "radial-gradient(ellipse 60% 60% at 50% 50%, transparent 20%, color-mix(in srgb, var(--background) 80%, transparent) 100%)",
        }}
      />

      {/* Player panel */}
      <section className="relative z-10 flex w-full max-w-[400px] flex-col overflow-hidden rounded-2xl border border-border bg-card/95 shadow-[var(--shadow)] backdrop-blur-sm">

        {/* Header */}
        <header className="flex items-center gap-3 border-b border-border px-5 py-4">
          <Tooltip>
            <TooltipTrigger asChild>
              <div
                className="flex size-9 shrink-0 items-center justify-center rounded-xl"
                style={{ background: "var(--orange-dim)", color: "var(--color-primary)" }}
              >
                <PlayIcon className="size-[18px]" fill="currentColor" strokeWidth={0} />
              </div>
            </TooltipTrigger>
            <TooltipContent side="bottom">
              {import.meta.env.VITE_SERVICE_VERSION ?? "dev"}
            </TooltipContent>
          </Tooltip>
          <div className="min-w-0 flex-1">
            <h1 className="text-[0.95rem] font-semibold leading-tight text-foreground">Sendspin Player</h1>
            <p className="text-[0.72rem] text-muted-foreground">Synchronized live audio</p>
          </div>
          <div className="flex shrink-0 items-center gap-[0.45rem]">
            <Button
              variant="ghost"
              size="icon-sm"
              aria-label="Share browser player"
              title="Share browser player"
              onClick={() => setShareOpen(true)}
            >
              <Share2Icon />
            </Button>
            <Button
              variant="outline"
              size="sm"
              className={
                connected
                  ? "min-w-[88px] active:scale-[0.98] border-destructive/40 bg-destructive/8 text-destructive hover:bg-destructive/12 hover:border-destructive/60"
                  : "min-w-[88px] active:scale-[0.98] border-primary/40 bg-primary/8 text-primary hover:bg-primary/13 hover:border-primary"
              }
              onClick={connected ? disconnect : connect}
            >
              {connected ? "Disconnect" : "Connect"}
            </Button>
          </div>
        </header>

        {/* Status hero */}
        <section
          className="relative flex flex-col items-center gap-[0.55rem] overflow-hidden border-b border-border px-5 py-5"
          aria-live="polite"
        >
          <VisualizerCanvas analyserRef={analyserRef} playing={state === "playing"} />
          <div className="relative z-10"><StatusRing state={state} /></div>
          <div
            key={state}
            className={`relative z-10 flex items-center gap-[0.4rem] rounded-full border px-[0.65rem] py-[0.22rem] text-[0.74rem] font-medium backdrop-blur-sm animate-[status-appear_0.25s_ease-out_both] ${
              state === "connected"
                ? "border-success/40 bg-card/90 text-success"
                : state === "connecting" || state === "reconnecting"
                  ? "border-warning/40 bg-card/90 text-warning"
                  : state === "playing"
                    ? "border-primary/40 bg-card/90 text-primary"
                    : state === "error"
                      ? "border-destructive/40 bg-card/90 text-destructive"
                      : "border-border bg-card/90 text-muted-foreground"
            }`}
          >
            <span
              className={`size-[6px] shrink-0 rounded-full ${
                state === "connected"
                  ? "bg-success"
                  : state === "connecting" || state === "reconnecting"
                    ? "animate-pulse bg-warning"
                    : state === "playing"
                      ? "animate-pulse bg-primary"
                      : state === "error"
                        ? "bg-destructive"
                        : "bg-stroke-muted"
              }`}
            />
            {statusText}
          </div>
        </section>

        {/* Server collapsible */}
        <Collapsible open={serverOpen} onOpenChange={setServerOpen}>
          <CollapsibleTrigger className="flex w-full cursor-pointer items-center justify-between border-b border-border px-5 py-[0.68rem] text-left transition-colors hover:bg-muted/60">
            <span className="text-[0.72rem] font-semibold uppercase tracking-[0.06em] text-muted-foreground">
              Server
            </span>
            <span className="flex min-w-0 flex-1 items-center justify-end gap-[0.65rem] pl-3">
              <span className="min-w-0 truncate text-right font-mono text-[0.74rem] text-muted-foreground">
                {baseUrl}
              </span>
              <ChevronRightIcon
                className={`size-[10px] shrink-0 text-muted-foreground transition-transform duration-[180ms] ${serverOpen ? "rotate-90" : ""}`}
              />
            </span>
          </CollapsibleTrigger>
          <CollapsibleContent>
            <div className="border-b border-border bg-muted/40 px-5 py-3">
              <Label htmlFor="server-url" className="mb-[0.35rem] block text-[0.75rem] text-muted-foreground">
                Server URL
              </Label>
              <Input
                id="server-url"
                aria-label="Server URL"
                value={baseUrl}
                disabled={connected}
                spellCheck={false}
                autoCorrect="off"
                autoCapitalize="off"
                inputMode="url"
                className="h-9 bg-card font-mono text-[0.78rem] disabled:opacity-60"
                onChange={(e) => setBaseUrl(e.target.value)}
              />
              <div className="mt-2 flex items-center justify-end gap-1">
              <Tooltip>
                <TooltipTrigger asChild>
                  <Button
                    variant="ghost"
                    size="xs"
                    className="h-6 px-2 text-[0.68rem] text-muted-foreground hover:text-foreground"
                    disabled={connected}
                    aria-label="Use default server URL"
                    onClick={resetServerUrl}
                  >
                    <RotateCcwIcon className="size-3" />
                    Default
                  </Button>
                </TooltipTrigger>
                <TooltipContent side="top">Use default server URL</TooltipContent>
              </Tooltip>
              <Tooltip>
                <TooltipTrigger asChild>
                  <Button
                    variant="ghost"
                    size="xs"
                    className="h-6 px-2 text-[0.68rem] text-muted-foreground hover:text-foreground"
                    disabled={connected}
                    aria-label="Use Sendspin demo server"
                    onClick={useDemoServerUrl}
                  >
                    <FlaskConicalIcon className="size-3" />
                    Demo
                  </Button>
                </TooltipTrigger>
                <TooltipContent side="top">Use Sendspin demo server</TooltipContent>
              </Tooltip>
              </div>
            </div>
          </CollapsibleContent>
        </Collapsible>

        {/* Controls */}
        <div className="flex flex-col gap-3.5 px-5 py-4">
          {/* Volume — single flex row at all screen sizes */}
          <div className="flex flex-col gap-1.5">
            <Label className="text-[0.75rem] text-muted-foreground">Volume</Label>
            <div className="flex items-center gap-2.5">
              <Slider
                min={0}
                max={100}
                value={[volume]}
                onValueChange={([v]) => updateVolume(v)}
                className="flex-1"
              />
              <output className="w-9 shrink-0 text-right font-mono text-[0.8rem] tabular-nums text-muted-foreground">
                {volume}%
              </output>
              <Button
                variant="outline"
                aria-pressed={muted}
                className={`h-8 shrink-0 gap-1.5 px-3 text-[0.75rem] ${
                  muted
                    ? "border-destructive/50 bg-destructive/12 text-destructive hover:bg-destructive/20"
                    : ""
                }`}
                onClick={() => updateMuted(!muted)}
              >
                {muted ? <VolumeXIcon className="size-3.5" /> : <Volume2Icon className="size-3.5" />}
                {muted ? "Muted" : "Mute"}
              </Button>
            </div>
          </div>

          {/* Sync mode */}
          <div className="flex flex-col gap-1.5">
            <Label className="text-[0.75rem] text-muted-foreground">Sync mode</Label>
            <Select
              value={correctionMode}
              onValueChange={(v) => updateCorrectionMode(v as CorrectionMode)}
            >
              <SelectTrigger className="!h-9 w-full text-[0.82rem]">
                <SelectValue />
              </SelectTrigger>
              <SelectContent position="popper" align="start">
                <SelectItem value="sync">Sync</SelectItem>
                <SelectItem value="quality">Quality</SelectItem>
                <SelectItem value="quality-local">Quality local</SelectItem>
              </SelectContent>
            </Select>
            <p className="text-[0.71rem] leading-snug text-muted-foreground/70">
              {CORRECTION_DESCRIPTIONS[correctionMode]}
            </p>
          </div>
        </div>


        {/* Diagnostics collapsible */}
        <Collapsible open={diagnosticsOpen} onOpenChange={setDiagnosticsOpen}>
          <CollapsibleTrigger className="flex w-full cursor-pointer items-center justify-between border-t border-border px-5 py-[0.68rem] text-left transition-colors hover:bg-muted/60">
            <span className="text-[0.72rem] font-semibold uppercase tracking-[0.06em] text-muted-foreground">
              Diagnostics
            </span>
            <ChevronRightIcon
              className={`size-[10px] text-muted-foreground transition-transform duration-[180ms] ${diagnosticsOpen ? "rotate-90" : ""}`}
            />
          </CollapsibleTrigger>
          <CollapsibleContent>
            <div
              className="grid grid-cols-2 border-t border-border bg-card"
              aria-label="Diagnostics"
            >
              <Metric label="Format" value={formatStream(snapshot.format)} />
              <Metric label="Time sync" value={snapshot.timeSynced ? "yes" : "no"} />
              <Metric label="Sync error" value={formatNumber(snapshot.syncErrorMs, " ms")} />
              <Metric label="Output latency" value={formatNumber(snapshot.outputLatencyMs, " ms")} />
              <Metric label="Correction" value={snapshot.correctionMethod} />
              <Metric
                label="Rate"
                value={snapshot.playbackRate === null ? "-" : snapshot.playbackRate.toFixed(3)}
              />
            </div>
          </CollapsibleContent>
        </Collapsible>

        {/* Activity log */}
        <section
          ref={logRef}
          className="flex h-[128px] flex-col overflow-y-auto border-t border-border bg-background/60 px-5 py-[0.65rem] font-mono text-[0.7rem] text-muted-foreground"
          aria-label="Activity log"
        >
          {logs.length === 0 ? (
            <p className="italic opacity-60">No activity yet</p>
          ) : (
            logs.map((entry) => (
              <p
                key={entry.id}
                className={`mb-[0.2rem] leading-[1.55] ${
                  entry.kind === "play"
                    ? "text-primary"
                    : entry.kind === "warn"
                      ? "text-warning"
                      : entry.kind === "error"
                        ? "text-destructive"
                        : "text-muted-foreground"
                }`}
              >
                {entry.message}
              </p>
            ))
          )}
        </section>

        {/* Footer */}
        <footer className="border-t border-border bg-card px-5 py-[0.55rem] text-center text-[0.68rem] text-muted-foreground/70">
          {"Powered by "}
          <a
            href="https://www.sendspin-audio.com/"
            target="_blank"
            rel="noopener noreferrer"
            className="font-semibold text-success hover:underline"
          >
            Sendspin
          </a>
          {" and "}
          <a
            href="https://www.openhomefoundation.org/"
            target="_blank"
            rel="noopener noreferrer"
            className="font-semibold text-success hover:underline"
          >
            Open Home Foundation
          </a>
        </footer>
      </section>

      {/* Share dialog */}
      <Dialog open={shareOpen} onOpenChange={setShareOpen}>
        <DialogContent className="sm:max-w-[360px]">
          <DialogHeader>
            <DialogTitle>Share browser player</DialogTitle>
          </DialogHeader>
          <p className="text-center text-[0.72rem] leading-[1.45] text-muted-foreground">
            Scan this code on another device to open this Sendspin player and join the same audio session there.
          </p>
          <QrCodeSvg text={shareUrl} />
          <p className="mt-[-0.35rem] break-all text-center font-mono text-[0.72rem] leading-[1.4] text-muted-foreground">
            {shareUrl}
          </p>
        </DialogContent>
      </Dialog>
    </main>
  );
}
