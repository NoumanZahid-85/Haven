"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import { Microphone, PaperPlaneRight, Stop, Lifebuoy } from "@phosphor-icons/react";
import { api } from "@/lib/api";
import { useIdentity } from "@/components/auth";
import type { Risk, TurnMetrics, VoiceMode } from "@/lib/types";
import { formatMs, formatTime } from "@/lib/format";

type VoiceState = "idle" | "connecting" | "listening" | "thinking" | "speaking" | "error";

interface Message {
  id: string;
  role: "user" | "agent";
  text: string;
  ts: Date;
  interrupted?: boolean;
  firstAudioMs?: number | null;
}

const MODES: { id: VoiceMode; label: string; hint: string }[] = [
  {
    id: "live",
    label: "Natural",
    hint: "Haven hears you and answers in one step (Gemini speech-to-speech). The most natural feel.",
  },
  {
    id: "pipeline",
    label: "Checked",
    hint: "Haven writes down what you said, thinks, then speaks. Every sentence is safety-checked before you hear it.",
  },
];

const STATE_TEXT: Record<VoiceState, string> = {
  idle: "Tap the light to talk",
  connecting: "Waking up…",
  listening: "I'm listening",
  thinking: "Thinking…",
  speaking: "Haven is speaking",
  error: "Can't connect right now",
};

const OUT_RATE = 24000;
const STORE = { session: "haven:session-id", messages: "haven:messages", mode: "haven:mode", bargeIn: "haven:barge-in" };

function readStore(key: string): string | null {
  try {
    return window.localStorage.getItem(key);
  } catch {
    return null;
  }
}

function writeStore(key: string, value: string) {
  try {
    window.localStorage.setItem(key, value);
  } catch {
    /* preference just won't persist */
  }
}

function useSessionId() {
  const [id] = useState(() => {
    if (typeof window === "undefined") return `sess-${Math.random().toString(36).slice(2)}`;
    const existing = window.sessionStorage.getItem(STORE.session);
    if (existing) return existing;
    const fresh = `sess-${Math.random().toString(36).slice(2)}`;
    window.sessionStorage.setItem(STORE.session, fresh);
    return fresh;
  });
  return id;
}

// ── Audio helpers ──────────────────────────────────────────────────────
// Up: mic → 16 kHz PCM16 frames. Down: 24 kHz PCM16 chunks, scheduled
// back-to-back on the AudioContext clock so streamed speech has no gaps.

function toPcm16(input: Float32Array, fromRate: number): Int16Array {
  const ratio = fromRate / 16000;
  const outLen = Math.floor(input.length / ratio);
  const out = new Int16Array(outLen);
  for (let i = 0; i < outLen; i++) {
    const pos = i * ratio;
    const idx = Math.floor(pos);
    const a = input[idx] ?? 0;
    const b = input[idx + 1] ?? a;
    const s = Math.max(-1, Math.min(1, a + (b - a) * (pos - idx)));
    out[i] = s < 0 ? s * 0x8000 : s * 0x7fff;
  }
  return out;
}

function pcmToBuffer(ctx: AudioContext, data: ArrayBuffer): AudioBuffer {
  const pcm = new Int16Array(data);
  const buffer = ctx.createBuffer(1, pcm.length, OUT_RATE);
  const channel = buffer.getChannelData(0);
  for (let i = 0; i < pcm.length; i++) channel[i] = pcm[i] / 0x8000;
  return buffer;
}

export default function TalkPage() {
  const sessionId = useSessionId();
  const { identity } = useIdentity();
  const userId = identity?.customerId ?? "";

  const [mode, setMode] = useState<VoiceMode>(() =>
    typeof window !== "undefined" && readStore(STORE.mode) === "pipeline" ? "pipeline" : "live",
  );
  const [bargeIn, setBargeIn] = useState(() => typeof window !== "undefined" && readStore(STORE.bargeIn) === "1");
  const [state, setState] = useState<VoiceState>("idle");
  const [error, setError] = useState<string | null>(null);
  const [risk, setRisk] = useState<Risk>("none");
  const [partial, setPartial] = useState("");
  const [metrics, setMetrics] = useState<TurnMetrics | null>(null);
  const [input, setInput] = useState("");
  const [sending, setSending] = useState(false);
  const [messages, setMessages] = useState<Message[]>(() => {
    if (typeof window === "undefined") return [];
    try {
      const raw = window.sessionStorage.getItem(STORE.messages);
      const parsed = raw ? (JSON.parse(raw) as (Omit<Message, "ts"> & { ts: string })[]) : [];
      return parsed.map((m) => ({ ...m, ts: new Date(m.ts) }));
    } catch {
      return [];
    }
  });

  const lampRef = useRef<HTMLButtonElement>(null);
  const bottomRef = useRef<HTMLDivElement>(null);
  const wsRef = useRef<WebSocket | null>(null);
  const ctxRef = useRef<AudioContext | null>(null);
  const micRef = useRef<MediaStream | null>(null);
  const sourcesRef = useRef(new Set<AudioBufferSourceNode>());
  const playEndRef = useRef(0); // AudioContext time when queued speech ends
  const finishTimer = useRef<ReturnType<typeof setTimeout> | null>(null);
  const turnRef = useRef(0);
  const replyEndedRef = useRef(false);
  const droppingRef = useRef(false); // after an interrupt, ignore stray audio until the next turn
  const floorRef = useRef(false); // Haven has the floor: in half-duplex the mic sends silence
  const bargeRef = useRef(bargeIn);
  const stateRef = useRef<VoiceState>("idle");

  useEffect(() => {
    stateRef.current = state;
  }, [state]);
  useEffect(() => {
    bargeRef.current = bargeIn;
  }, [bargeIn]);
  useEffect(() => {
    bottomRef.current?.scrollIntoView({ behavior: "smooth", block: "nearest" });
    try {
      window.sessionStorage.setItem(STORE.messages, JSON.stringify(messages.slice(-60)));
    } catch {
      /* conversation just won't survive navigation */
    }
  }, [messages]);

  const push = useCallback((m: Omit<Message, "id" | "ts">) => {
    setMessages((prev) => [...prev, { ...m, id: `${Date.now()}-${prev.length}`, ts: new Date() }]);
  }, []);

  const patchLastAgent = useCallback((patch: (m: Message) => Partial<Message>) => {
    setMessages((prev) => {
      for (let i = prev.length - 1; i >= 0; i--) {
        if (prev[i].role === "agent") {
          const next = [...prev];
          next[i] = { ...prev[i], ...patch(prev[i]) };
          return next;
        }
      }
      return prev;
    });
  }, []);

  // ── Playback ───────────────────────────────────────────────────────
  const stopPlayback = useCallback(() => {
    sourcesRef.current.forEach((s) => {
      try {
        s.stop();
      } catch {
        /* already ended */
      }
    });
    sourcesRef.current.clear();
    playEndRef.current = ctxRef.current?.currentTime ?? 0;
    if (finishTimer.current) clearTimeout(finishTimer.current);
  }, []);

  const finishTurn = useCallback(() => {
    floorRef.current = false;
    const ws = wsRef.current;
    if (ws?.readyState === WebSocket.OPEN) ws.send(JSON.stringify({ type: "playback_done", turn: turnRef.current }));
    if (stateRef.current === "speaking") setState("listening");
  }, []);

  const scheduleFinish = useCallback(() => {
    const ctx = ctxRef.current;
    if (!ctx || !replyEndedRef.current) return;
    if (finishTimer.current) clearTimeout(finishTimer.current);
    const remaining = Math.max(0, playEndRef.current - ctx.currentTime);
    finishTimer.current = setTimeout(finishTurn, remaining * 1000 + 80);
  }, [finishTurn]);

  const playChunk = useCallback(
    (data: ArrayBuffer) => {
      const ctx = ctxRef.current;
      if (!ctx || droppingRef.current || data.byteLength < 2) return;
      const buffer = pcmToBuffer(ctx, data);
      const src = ctx.createBufferSource();
      src.buffer = buffer;
      src.connect(ctx.destination);
      const at = Math.max(ctx.currentTime + 0.03, playEndRef.current);
      src.start(at);
      playEndRef.current = at + buffer.duration;
      sourcesRef.current.add(src);
      src.onended = () => sourcesRef.current.delete(src);
      if (stateRef.current !== "speaking") setState("speaking");
      scheduleFinish();
    },
    [scheduleFinish],
  );

  // ── Session ────────────────────────────────────────────────────────
  const teardown = useCallback(() => {
    try {
      wsRef.current?.close();
    } catch {
      /* best-effort */
    }
    wsRef.current = null;
    stopPlayback();
    micRef.current?.getTracks().forEach((t) => t.stop());
    micRef.current = null;
    ctxRef.current?.close().catch(() => {});
    ctxRef.current = null;
    floorRef.current = false;
    lampRef.current?.style.setProperty("--level", "0");
  }, [stopPlayback]);

  const stop = useCallback(() => {
    teardown();
    setState("idle");
    setPartial("");
  }, [teardown]);

  const start = useCallback(async () => {
    setError(null);
    setState("connecting");
    let mic: MediaStream;
    try {
      mic = await navigator.mediaDevices.getUserMedia({
        audio: { echoCancellation: true, noiseSuppression: true, autoGainControl: true },
      });
    } catch {
      setState("error");
      setError("Microphone access is blocked. Allow it in your browser's site settings, then tap the light again.");
      return;
    }
    micRef.current = mic;
    const ctx = new AudioContext();
    if (ctx.state === "suspended") await ctx.resume().catch(() => {});
    ctxRef.current = ctx;
    playEndRef.current = ctx.currentTime;

    const ws = new WebSocket(api.voice.wsUrl(mode, `${sessionId}-${Date.now().toString(36)}`, userId, bargeIn));
    ws.binaryType = "arraybuffer";
    wsRef.current = ws;

    ws.onmessage = (ev: MessageEvent<string | ArrayBuffer>) => {
      if (typeof ev.data !== "string") {
        playChunk(ev.data);
        return;
      }
      let msg: { type?: string; text?: string; message?: string; turn?: number; level?: Risk } & Partial<TurnMetrics>;
      try {
        msg = JSON.parse(ev.data);
      } catch {
        return;
      }
      switch (msg.type) {
        case "ready":
          if (stateRef.current === "connecting") setState("listening");
          break;
        case "user_partial":
          setPartial(msg.text ?? "");
          break;
        case "transcript":
          if (msg.text) push({ role: "user", text: msg.text });
          setPartial("");
          setState("thinking");
          break;
        case "speaking_start":
          turnRef.current = msg.turn ?? turnRef.current + 1;
          replyEndedRef.current = false;
          droppingRef.current = false;
          floorRef.current = true;
          push({ role: "agent", text: "" });
          break;
        case "agent_caption":
          if (msg.text) patchLastAgent((m) => ({ text: m.text + msg.text }));
          break;
        case "reply_end":
          replyEndedRef.current = true;
          scheduleFinish();
          break;
        case "interrupt":
          droppingRef.current = true;
          stopPlayback();
          patchLastAgent(() => ({ interrupted: true }));
          floorRef.current = false;
          setState("listening");
          break;
        case "metrics":
          setMetrics(msg as TurnMetrics);
          patchLastAgent(() => ({ firstAudioMs: msg.first_audio_ms ?? null }));
          break;
        case "risk":
          if (msg.level === "crisis") setRisk("crisis");
          break;
        case "error":
          setError(msg.message ?? "Something went wrong with the voice connection.");
          break;
      }
    };
    ws.onclose = () => {
      if (stateRef.current !== "error") setState("idle");
    };
    ws.onerror = () => {
      setState("error");
      setError("Couldn't reach Haven. The server may be waking up (about a minute on the free plan). Try again shortly.");
    };

    // Mic → 16 kHz PCM16. While Haven has the floor in half-duplex mode we
    // send silence instead, so its own voice can never be heard as yours.
    const source = ctx.createMediaStreamSource(mic);
    const processor = ctx.createScriptProcessor(4096, 1, 1);
    const mute = ctx.createGain();
    mute.gain.value = 0;
    processor.onaudioprocess = (e) => {
      const samples = e.inputBuffer.getChannelData(0);
      let sum = 0;
      for (let i = 0; i < samples.length; i += 16) sum += samples[i] * samples[i];
      const level = Math.min(1, Math.sqrt(sum / (samples.length / 16)) * 9);
      lampRef.current?.style.setProperty("--level", floorRef.current ? "0" : level.toFixed(3));
      if (ws.readyState !== WebSocket.OPEN) return;
      const pcm = toPcm16(samples, ctx.sampleRate);
      ws.send(floorRef.current && !bargeRef.current ? new Int16Array(pcm.length).buffer : pcm.buffer);
    };
    source.connect(processor);
    processor.connect(mute);
    mute.connect(ctx.destination);
  }, [mode, sessionId, userId, bargeIn, playChunk, push, patchLastAgent, scheduleFinish, stopPlayback]);

  useEffect(() => teardown, [teardown]);

  // ── Text ───────────────────────────────────────────────────────────
  const sendText = useCallback(async () => {
    const text = input.trim();
    if (!text || sending) return;
    setInput("");
    push({ role: "user", text });
    setSending(true);
    try {
      const data = await api.chat(text, sessionId, userId);
      if (data.risk === "crisis") setRisk("crisis");
      push({ role: "agent", text: data.reply });
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setSending(false);
    }
  }, [input, sending, sessionId, userId, push]);

  const live = state !== "idle" && state !== "error";
  const current = MODES.find((m) => m.id === mode)!;
  const lastAgent = [...messages].reverse().find((m) => m.role === "agent" && m.text);

  let sub = "Uses your microphone. You can also type below.";
  if (state === "listening") sub = "Take your time. Pause when you're done.";
  if (state === "speaking") sub = bargeIn ? "Talk anytime to interrupt." : "Let me finish, then it's your turn.";
  if (state === "connecting") sub = "Connecting to Haven.";

  return (
    <div className="talk">
      {risk === "crisis" && (
        <div className="crisis-banner" role="alert">
          <Lifebuoy size={20} weight="duotone" />
          <p>
            If you&apos;re in danger, call your local emergency number. In the US, call or text <strong>988</strong>. In the
            UK and Ireland, call Samaritans on <strong>116 123</strong>. Elsewhere,{" "}
            <a href="https://findahelpline.com" target="_blank" rel="noreferrer">
              findahelpline.com
            </a>{" "}
            lists free lines in your country.
          </p>
        </div>
      )}

      <section className="talk-stage" aria-live="polite">
        <div className="mode-picker">
          <div role="radiogroup" aria-label="Voice mode" style={{ display: "flex", gap: 8 }}>
            {MODES.map((m) => (
              <button
                key={m.id}
                className="pill"
                role="radio"
                aria-checked={mode === m.id}
                disabled={live}
                onClick={() => {
                  setMode(m.id);
                  writeStore(STORE.mode, m.id);
                }}
              >
                {m.label}
              </button>
            ))}
          </div>
          <label className="toggle" title="Best with headphones, so Haven doesn't hear itself">
            <input
              type="checkbox"
              checked={bargeIn}
              disabled={live}
              onChange={() => {
                setBargeIn(!bargeIn);
                writeStore(STORE.bargeIn, bargeIn ? "0" : "1");
              }}
            />
            <span className="toggle-track" aria-hidden="true" />
            Let me interrupt
          </label>
        </div>
        <p className="mode-hint">{current.hint}</p>

        <button
          ref={lampRef}
          className={`lamp ${live ? `lamp-${state}` : "lamp-off"}`}
          onClick={live ? stop : start}
          disabled={state === "connecting"}
          aria-label={live ? "End the conversation" : "Start talking"}
        >
          {live ? <Stop size={30} weight="fill" /> : <Microphone size={34} weight="fill" />}
        </button>

        <h1 className="lamp-state">{STATE_TEXT[state]}</h1>
        <p className="lamp-sub">{sub}</p>

        {error && <p className="talk-error">{error}</p>}

        {live && (
          <p className={`live-caption ${partial ? "live-caption-user" : ""}`}>
            {partial ? `“${partial}”` : state === "speaking" ? lastAgent?.text : ""}
          </p>
        )}

        {metrics?.first_audio_ms != null && (
          <p
            className="latency"
            title={`End of turn detected ${formatMs(metrics.stt_ms)}, LLM first token ${formatMs(metrics.llm_first_token_ms)}`}
          >
            Haven started answering <strong>{formatMs(metrics.first_audio_ms)}</strong> after you stopped speaking
          </p>
        )}
      </section>

      <section className="thread" aria-label="Conversation">
        {messages.some((m) => m.text) && <h2 className="thread-title">This conversation</h2>}
        {messages
          .filter((m) => m.text)
          .map((m) => (
            <div key={m.id} style={{ display: "contents" }}>
              <div className={`msg ${m.role === "user" ? "msg-user" : "msg-agent"}`}>
                {m.text.trim()}
                {m.interrupted && <span style={{ color: "var(--haze-2)" }}> (you interrupted)</span>}
              </div>
              <span className="msg-meta" style={{ alignSelf: m.role === "user" ? "flex-end" : "flex-start" }}>
                {formatTime(m.ts)}
                {m.firstAudioMs ? `, answered in ${formatMs(m.firstAudioMs)}` : ""}
              </span>
            </div>
          ))}
        <div ref={bottomRef} />
      </section>

      <form
        className="composer"
        onSubmit={(e) => {
          e.preventDefault();
          void sendText();
        }}
      >
        <input
          className="input-field"
          value={input}
          onChange={(e) => setInput(e.target.value)}
          placeholder="Can't talk out loud? Type here"
          aria-label="Message"
          autoComplete="off"
          disabled={sending}
        />
        <button className="btn-primary" type="submit" disabled={sending || !input.trim()} aria-label="Send">
          <PaperPlaneRight size={18} weight="fill" />
        </button>
      </form>
    </div>
  );
}
