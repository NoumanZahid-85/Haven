"use client";

import { useState, useRef, useEffect, useCallback } from "react";
import { motion, AnimatePresence } from "motion/react";
import {
  Microphone,
  Stop,
  PaperPlaneTilt,
  Spinner,
  Leaf,
  Lifebuoy,
  Timer,
  WarningCircle,
} from "@phosphor-icons/react";
import { api } from "@/lib/api";
import { useIdentity } from "@/components/auth";
import type { Risk, TurnMetrics } from "@/lib/types";
import { PageHeader } from "@/app/components/ui";
import { formatMs, formatTime } from "@/lib/format";

type VoiceState = "idle" | "connecting" | "listening" | "thinking" | "speaking" | "error";

interface Message {
  id: string;
  role: "user" | "agent";
  text: string;
  ts: Date;
  interrupted?: boolean;
  metrics?: TurnMetrics;
}

const SESSION_STORAGE_KEY = "haven:session-id";
const MESSAGES_STORAGE_KEY = "haven:messages";
const BARGE_IN_KEY = "haven:barge-in";

const STATE_LABEL: Record<VoiceState, string> = {
  idle: "Tap to start talking",
  connecting: "Connecting…",
  listening: "Listening — take your time",
  thinking: "Thinking…",
  speaking: "Haven is speaking",
  error: "Voice unavailable",
};

function useSessionId() {
  const [id] = useState(() => {
    if (typeof window === "undefined") return `sess-${Math.random().toString(36).slice(2)}`;
    const existing = window.sessionStorage.getItem(SESSION_STORAGE_KEY);
    if (existing) return existing;
    const fresh = `sess-${Math.random().toString(36).slice(2)}`;
    window.sessionStorage.setItem(SESSION_STORAGE_KEY, fresh);
    return fresh;
  });
  return id;
}

// ── Audio helpers ──────────────────────────────────────────────────────
// The browser streams PCM16 mono @16 kHz over the WebSocket; the backend
// runs Silero VAD → Whisper → companion → Orpheus and returns one WAV per
// sentence, which we decode and play strictly in order.

function floatToInt16(input: Float32Array): Int16Array {
  const out = new Int16Array(input.length);
  for (let i = 0; i < input.length; i++) {
    const s = Math.max(-1, Math.min(1, input[i]));
    out[i] = s < 0 ? s * 0x8000 : s * 0x7fff;
  }
  return out;
}

function resampleTo16k(input: Float32Array, fromRate: number): Float32Array {
  const ratio = fromRate / 16000;
  if (Math.abs(ratio - 1) < 0.001) return input;
  const outLen = Math.floor(input.length / ratio);
  const out = new Float32Array(outLen);
  for (let i = 0; i < outLen; i++) {
    const pos = i * ratio;
    const idx = Math.floor(pos);
    const frac = pos - idx;
    const a = input[idx] ?? 0;
    const b = input[idx + 1] ?? a;
    out[i] = a + (b - a) * frac;
  }
  return out;
}

export default function TalkPage() {
  const sessionId = useSessionId();
  const { identity } = useIdentity();
  const userId = identity?.customerId ?? "";

  const [messages, setMessages] = useState<Message[]>(() => {
    if (typeof window === "undefined") return [];
    try {
      const raw = window.sessionStorage.getItem(MESSAGES_STORAGE_KEY);
      const parsed = raw ? (JSON.parse(raw) as (Omit<Message, "ts"> & { ts: string })[]) : [];
      return parsed.map((m) => ({ ...m, ts: new Date(m.ts) }));
    } catch {
      return [];
    }
  });
  const [input, setInput] = useState("");
  const [sending, setSending] = useState(false);
  const [voiceState, setVoiceState] = useState<VoiceState>("idle");
  const [connError, setConnError] = useState<string | null>(null);
  const [risk, setRisk] = useState<Risk>("none");
  const [lastMetrics, setLastMetrics] = useState<TurnMetrics | null>(null);
  const [bargeIn, setBargeIn] = useState(() => {
    if (typeof window === "undefined") return false;
    try {
      return window.localStorage.getItem(BARGE_IN_KEY) === "1";
    } catch {
      return false;
    }
  });

  const bottomRef = useRef<HTMLDivElement>(null);
  const orbRef = useRef<HTMLButtonElement>(null);
  const wsRef = useRef<WebSocket | null>(null);
  const audioCtxRef = useRef<AudioContext | null>(null);
  const micStreamRef = useRef<MediaStream | null>(null);
  const watchdogRef = useRef<ReturnType<typeof setInterval> | null>(null);
  // Playback: decoded WAV buffers awaiting sequential play.
  const playQueueRef = useRef<AudioBuffer[]>([]);
  const currentSourceRef = useRef<AudioBufferSourceNode | null>(null);
  const playingRef = useRef(false);
  // Bumped on interrupt so WAVs still decoding from the old reply are dropped.
  const epochRef = useRef(0);
  const turnRef = useRef(0);
  // True while Haven has the floor in half-duplex mode — mic is not streamed.
  const gateRef = useRef(false);
  const gateOpenedAtRef = useRef(0);
  const voiceStateRef = useRef<VoiceState>("idle");
  useEffect(() => {
    voiceStateRef.current = voiceState;
  }, [voiceState]);

  useEffect(() => {
    bottomRef.current?.scrollIntoView({ behavior: "smooth" });
  }, [messages]);

  useEffect(() => {
    try {
      window.sessionStorage.setItem(MESSAGES_STORAGE_KEY, JSON.stringify(messages.slice(-60)));
    } catch {
      /* storage unavailable — conversation just won't persist across navigation */
    }
  }, [messages]);

  const pushMessage = useCallback((m: Omit<Message, "id" | "ts">) => {
    setMessages((prev) => [...prev, { ...m, id: `${Date.now()}-${Math.random()}`, ts: new Date() }]);
  }, []);

  /** Append a caption to the newest agent message (one message per reply). */
  const appendAgent = useCallback((text: string) => {
    setMessages((prev) => {
      const last = prev[prev.length - 1];
      if (last?.role === "agent" && !last.interrupted && !last.metrics) {
        return [...prev.slice(0, -1), { ...last, text: `${last.text} ${text}`.trim() }];
      }
      return [...prev, { id: `${Date.now()}`, role: "agent", text, ts: new Date() }];
    });
  }, []);

  const annotateLastAgent = useCallback((patch: Partial<Message>) => {
    setMessages((prev) => {
      for (let i = prev.length - 1; i >= 0; i--) {
        if (prev[i].role === "agent") {
          const next = [...prev];
          next[i] = { ...prev[i], ...patch };
          return next;
        }
      }
      return prev;
    });
  }, []);

  // ── Text fallback ──────────────────────────────────────────────────
  const sendText = useCallback(async () => {
    const msg = input.trim();
    if (!msg || sending) return;
    setInput("");
    pushMessage({ role: "user", text: msg });
    setSending(true);
    try {
      const data = await api.chat(msg, sessionId, userId);
      if (data.risk !== "none") setRisk((r) => (r === "crisis" ? r : data.risk));
      pushMessage({ role: "agent", text: data.reply });
    } catch (err: unknown) {
      pushMessage({
        role: "agent",
        text: `I couldn't reach the server (${err instanceof Error ? err.message : String(err)}). Please try again in a moment.`,
      });
    } finally {
      setSending(false);
    }
  }, [input, sending, sessionId, userId, pushMessage]);

  // ── Voice session ──────────────────────────────────────────────────
  const teardown = useCallback(() => {
    try {
      wsRef.current?.close();
    } catch {
      /* best-effort */
    }
    wsRef.current = null;
    if (watchdogRef.current) clearInterval(watchdogRef.current);
    watchdogRef.current = null;
    micStreamRef.current?.getTracks().forEach((t) => t.stop());
    micStreamRef.current = null;
    audioCtxRef.current?.close().catch(() => {});
    audioCtxRef.current = null;
    playQueueRef.current = [];
    currentSourceRef.current = null;
    playingRef.current = false;
    gateRef.current = false;
    orbRef.current?.style.setProperty("--level", "0");
  }, []);

  const stopVoice = useCallback(() => {
    teardown();
    setVoiceState("idle");
    setConnError(null);
  }, [teardown]);

  const startVoice = useCallback(async () => {
    setVoiceState("connecting");
    setConnError(null);

    let micStream: MediaStream;
    try {
      micStream = await navigator.mediaDevices.getUserMedia({
        audio: { echoCancellation: true, noiseSuppression: true, autoGainControl: true },
      });
    } catch {
      setVoiceState("error");
      setConnError("Microphone access was blocked. Allow microphone permission for this site and try again.");
      return;
    }
    micStreamRef.current = micStream;

    // Default device rate (usually 48 kHz); we resample to 16 kHz ourselves.
    const audioCtx = new AudioContext();
    // A fresh AudioContext starts suspended under autoplay policy — this
    // click is the user gesture that unlocks both mic processing and playback.
    if (audioCtx.state === "suspended") await audioCtx.resume().catch(() => {});
    audioCtxRef.current = audioCtx;

    const ws = new WebSocket(api.voice.wsUrl(`${sessionId}-${Date.now().toString(36)}`, userId, bargeIn));
    ws.binaryType = "arraybuffer";
    wsRef.current = ws;

    const finishPlayback = () => {
      gateRef.current = false;
      if (voiceStateRef.current === "speaking") setVoiceState("listening");
      if (ws.readyState === WebSocket.OPEN) {
        ws.send(JSON.stringify({ type: "playback_done", turn: turnRef.current }));
      }
    };

    // The reply streams in sentence by sentence, so an empty queue is only
    // the end once the server has said reply_end AND every WAV it announced
    // (via agent_caption) has been decoded and played.
    let awaitingMore = false;
    let pendingWavs = 0;
    const maybeFinish = () => {
      if (!awaitingMore && pendingWavs === 0 && !playingRef.current && playQueueRef.current.length === 0) {
        finishPlayback();
      }
    };

    const pump = () => {
      if (playingRef.current) return;
      const next = playQueueRef.current.shift();
      if (!next) return maybeFinish();
      playingRef.current = true;
      const src = audioCtx.createBufferSource();
      src.buffer = next;
      src.onended = () => {
        playingRef.current = false;
        currentSourceRef.current = null;
        pump();
      };
      src.connect(audioCtx.destination);
      currentSourceRef.current = src;
      src.start();
    };

    ws.onmessage = async (ev: MessageEvent<string | ArrayBuffer>) => {
      if (typeof ev.data === "string") {
        let msg: { type?: string; text?: string; message?: string; turn?: number; level?: Risk } & Partial<TurnMetrics>;
        try {
          msg = JSON.parse(ev.data);
        } catch {
          return;
        }
        switch (msg.type) {
          case "transcript":
            if (msg.text) pushMessage({ role: "user", text: msg.text });
            setVoiceState("thinking");
            break;
          case "speaking_start":
            turnRef.current = msg.turn ?? turnRef.current + 1;
            gateRef.current = !bargeIn;
            gateOpenedAtRef.current = Date.now();
            awaitingMore = true;
            setMessages((prev) => [...prev, { id: `${Date.now()}`, role: "agent", text: "", ts: new Date() }]);
            break;
          case "agent_caption":
            if (msg.text) appendAgent(msg.text);
            pendingWavs++;
            break;
          case "reply_end":
            awaitingMore = false;
            maybeFinish();
            break;
          case "metrics":
            setLastMetrics(msg as TurnMetrics);
            annotateLastAgent({ metrics: msg as TurnMetrics });
            break;
          case "risk":
            if (msg.level) setRisk((r) => (r === "crisis" ? r : (msg.level as Risk)));
            break;
          case "interrupt":
            epochRef.current++;
            awaitingMore = false;
            pendingWavs = 0;
            playQueueRef.current = [];
            try {
              currentSourceRef.current?.stop();
            } catch {
              /* already stopped */
            }
            currentSourceRef.current = null;
            playingRef.current = false;
            annotateLastAgent({ interrupted: true });
            setVoiceState("listening");
            break;
          case "error":
            if (msg.message) setConnError(msg.message);
            break;
        }
        return;
      }
      // Binary frame = one complete WAV for the most recent caption.
      const epoch = epochRef.current;
      try {
        const buffer = await audioCtx.decodeAudioData(ev.data.slice(0));
        pendingWavs = Math.max(0, pendingWavs - 1);
        if (epoch !== epochRef.current) return; // reply was interrupted meanwhile
        if (voiceStateRef.current !== "speaking") setVoiceState("speaking");
        playQueueRef.current.push(buffer);
        pump();
      } catch {
        pendingWavs = Math.max(0, pendingWavs - 1);
        maybeFinish();
      }
    };

    ws.onerror = () => {
      if (voiceStateRef.current !== "idle") {
        setVoiceState("error");
        setConnError("Voice connection failed. Please try again.");
      }
    };
    ws.onclose = () => {
      setVoiceState((s) => (s === "error" ? s : "idle"));
    };

    // ── Mic capture: ScriptProcessor → resample 16k → PCM16 → WS ─────
    const sourceNode = audioCtx.createMediaStreamSource(micStream);
    const processor = audioCtx.createScriptProcessor(4096, 1, 1);
    const muteGain = audioCtx.createGain();
    muteGain.gain.value = 0; // processor must reach destination to run, silently
    processor.onaudioprocess = (e) => {
      const samples = e.inputBuffer.getChannelData(0);
      let sum = 0;
      for (let i = 0; i < samples.length; i += 16) sum += samples[i] * samples[i];
      const level = Math.min(1, Math.sqrt(sum / (samples.length / 16)) * 8);
      orbRef.current?.style.setProperty("--level", level.toFixed(3));
      if (gateRef.current || ws.readyState !== WebSocket.OPEN) return;
      ws.send(floatToInt16(resampleTo16k(samples, audioCtx.sampleRate)).buffer as ArrayBuffer);
    };
    sourceNode.connect(processor);
    processor.connect(muteGain);
    muteGain.connect(audioCtx.destination);

    // Safety: never leave the mic gated forever if playback stalls.
    watchdogRef.current = setInterval(() => {
      if (gateRef.current && Date.now() - gateOpenedAtRef.current > 60_000) finishPlayback();
    }, 3000);

    try {
      await new Promise<void>((resolve, reject) => {
        const timer = setTimeout(() => reject(new Error("Connection timed out — the server may be waking up. Try again.")), 20_000);
        ws.onopen = () => {
          clearTimeout(timer);
          resolve();
        };
      });
      setVoiceState("listening");
    } catch (err) {
      teardown();
      setVoiceState("error");
      setConnError(err instanceof Error ? err.message : String(err));
    }
  }, [sessionId, userId, bargeIn, teardown, pushMessage, appendAgent, annotateLastAgent]);

  useEffect(() => teardown, [teardown]);

  const live = voiceState !== "idle" && voiceState !== "error";
  const toggleBargeIn = () => {
    const next = !bargeIn;
    setBargeIn(next);
    try {
      window.localStorage.setItem(BARGE_IN_KEY, next ? "1" : "0");
    } catch {
      /* preference just won't persist */
    }
  };

  return (
    <div style={{ display: "flex", flexDirection: "column", height: "100dvh", minWidth: 0 }}>
      <PageHeader
        icon={<Leaf size={17} weight="duotone" />}
        title="Talk"
        subtitle="Private · not a substitute for professional care"
        actions={
          <label className="toggle" title="Talk over Haven to interrupt. Best with headphones.">
            <input type="checkbox" checked={bargeIn} onChange={toggleBargeIn} disabled={live} />
            <span className="toggle-track" aria-hidden="true" />
            <span className="toggle-label">Interruptions</span>
          </label>
        }
      />

      {risk === "crisis" && (
        <div className="crisis-banner" role="alert">
          <Lifebuoy size={18} weight="duotone" />
          <span>
            If you&apos;re in danger, call your local emergency number. US: call or text <strong>988</strong> · UK &amp; IE:
            Samaritans <strong>116 123</strong> · Elsewhere:{" "}
            <a href="https://findahelpline.com" target="_blank" rel="noreferrer">
              findahelpline.com
            </a>
          </span>
        </div>
      )}

      <div className="scroll-area talk-scroll" aria-live="polite">
        {/* ── Voice orb ── */}
        <div className="orb-wrap">
          <button
            ref={orbRef}
            className={`orb orb-${voiceState}`}
            onClick={live ? stopVoice : startVoice}
            disabled={voiceState === "connecting"}
            aria-label={live ? "End voice conversation" : "Start voice conversation"}
          >
            {voiceState === "connecting" ? (
              <Spinner size={30} className="blink" />
            ) : live ? (
              <Stop size={30} weight="fill" />
            ) : (
              <Microphone size={32} weight="fill" />
            )}
          </button>
          <div className="orb-label">{STATE_LABEL[voiceState]}</div>
          {connError && (
            <div className="orb-error">
              <WarningCircle size={14} weight="fill" /> {connError}
            </div>
          )}
          {lastMetrics && (
            <div className="metrics-strip" title="Measured from the moment you stopped speaking">
              <Timer size={13} />
              <span>STT {formatMs(lastMetrics.stt_ms)}</span>
              <span>LLM first token {formatMs(lastMetrics.llm_first_token_ms)}</span>
              <span>
                First audio <strong>{formatMs(lastMetrics.first_audio_ms)}</strong>
              </span>
            </div>
          )}
        </div>

        {/* ── Transcript ── */}
        <div className="talk-thread">
          <AnimatePresence initial={false}>
            {messages
              .filter((m) => m.text)
              .map((m) => (
                <motion.div
                  key={m.id}
                  initial={{ opacity: 0, y: 8 }}
                  animate={{ opacity: 1, y: 0 }}
                  transition={{ duration: 0.25, ease: [0.16, 1, 0.3, 1] }}
                  style={{ display: "flex", flexDirection: "column", alignItems: m.role === "user" ? "flex-end" : "flex-start" }}
                >
                  <div className={m.role === "user" ? "bubble-user" : "bubble-agent"} style={{ whiteSpace: "pre-wrap" }}>
                    {m.text}
                    {m.interrupted && <span className="interrupted-tag"> — you interrupted</span>}
                  </div>
                  <span className="mono-id" style={{ marginTop: 4, fontSize: "0.6rem" }}>
                    {formatTime(m.ts)}
                    {m.metrics?.first_audio_ms ? ` · first audio ${formatMs(m.metrics.first_audio_ms)}` : ""}
                  </span>
                </motion.div>
              ))}
          </AnimatePresence>
          {sending && (
            <span className="typing" role="status" aria-label="Haven is replying">
              <span />
              <span />
              <span />
            </span>
          )}
          <div ref={bottomRef} />
        </div>
      </div>

      {/* ── Text fallback ── */}
      <div className="talk-input">
        <input
          className="input-field"
          value={input}
          onChange={(e) => setInput(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === "Enter" && !e.shiftKey) {
              e.preventDefault();
              void sendText();
            }
          }}
          placeholder={live ? "Voice is on — or type here" : "Can't talk right now? Type instead…"}
          disabled={sending}
          aria-label="Message"
          lang="en"
          autoComplete="off"
        />
        <button
          className="btn-primary"
          onClick={() => void sendText()}
          disabled={sending || !input.trim()}
          style={{ flexShrink: 0, padding: "10px 14px" }}
          aria-label="Send message"
        >
          {sending ? <Spinner size={16} className="blink" /> : <PaperPlaneTilt size={16} weight="fill" />}
        </button>
      </div>
    </div>
  );
}
