"use client";

import Link from "next/link";
import { motion } from "motion/react";
import {
  ArrowRight,
  Brain,
  ChartLineUp,
  Leaf,
  Lightning,
  Lifebuoy,
  ShieldCheck,
  BookOpenText,
} from "@phosphor-icons/react";

const FEATURES = [
  {
    icon: <Lightning size={22} weight="duotone" />,
    title: "Real-time, and interruptible",
    body: "Replies stream sentence by sentence into speech, so Haven starts talking before it has finished thinking. Talk over it and it stops and listens.",
  },
  {
    icon: <ShieldCheck size={22} weight="duotone" />,
    title: "Safety on both sides of the model",
    body: "Crisis language bypasses the LLM entirely for a fixed response with helplines. Every spoken sentence is screened for diagnoses and medication advice.",
  },
  {
    icon: <Brain size={22} weight="duotone" />,
    title: "Remembers you — on your terms",
    body: "Durable facts carry across conversations so you don't start from zero. See exactly what's remembered and erase it in one click.",
  },
  {
    icon: <BookOpenText size={22} weight="duotone" />,
    title: "Grounded in evidence-based techniques",
    body: "Breathing, grounding, CBT reframing, ACT defusion, sleep and behavioural activation — retrieved only when they fit, offered one step at a time.",
  },
  {
    icon: <ChartLineUp size={22} weight="duotone" />,
    title: "Measured, evaluated, improved",
    body: "Every turn is stored with a latency breakdown. An LLM judge scores conversations and the weakest turns become regression tests.",
  },
];

const PIPELINE = [
  { step: "01", label: "You speak", note: "Browser mic → WebSocket · Silero VAD" },
  { step: "02", label: "Transcribe", note: "Groq Whisper large-v3-turbo" },
  { step: "03", label: "Screen & recall", note: "Safety tier · memory · technique RAG" },
  { step: "04", label: "Think", note: "LLM token stream → sentences" },
  { step: "05", label: "Speak", note: "Orpheus TTS per sentence, in order" },
];

const fade = {
  initial: { opacity: 0, y: 14 },
  whileInView: { opacity: 1, y: 0 },
  viewport: { once: true },
  transition: { duration: 0.5, ease: [0.16, 1, 0.3, 1] as const },
};

export default function Landing() {
  return (
    <div style={{ position: "relative", zIndex: 1 }}>
      <header className="container-page landing-nav">
        <div className="sidebar-brand" style={{ padding: 0, border: "none" }}>
          <Leaf size={20} color="var(--signal)" weight="fill" />
          <span>HAVEN</span>
        </div>
        <Link href="/talk" className="btn-primary btn-sm">
          Start talking <ArrowRight size={14} />
        </Link>
      </header>

      <main>
        <section className="container-page hero">
          <motion.p {...fade} className="label-mono" style={{ color: "var(--signal)" }}>
            Voice-first companion for mental wellbeing
          </motion.p>
          <motion.h1 {...fade} className="display-xl" style={{ maxWidth: 820 }}>
            Someone to talk to at 2:37 in the morning.
          </motion.h1>
          <motion.p {...fade} className="hero-sub">
            A night you can&apos;t sleep. Anxiety before work. A breakup. Haven is a real-time voice companion that
            listens first, remembers what matters to you, and knows when to point you to a human.
          </motion.p>
          <motion.div {...fade} style={{ display: "flex", gap: 10, flexWrap: "wrap" }}>
            <Link href="/talk" className="btn-primary">
              Start talking <ArrowRight size={15} />
            </Link>
            <a href="#how" className="btn-ghost">
              How it works
            </a>
          </motion.div>
          <motion.div {...fade} className="hero-note">
            <Lifebuoy size={16} weight="duotone" />
            <span>
              Haven is not a therapist and not an emergency service. If you&apos;re in danger, call your local emergency
              number — US <strong>988</strong> · UK &amp; IE <strong>116 123</strong> ·{" "}
              <a href="https://findahelpline.com" target="_blank" rel="noreferrer">
                findahelpline.com
              </a>
            </span>
          </motion.div>
        </section>

        <section id="how" className="container-page section">
          <motion.h2 {...fade} className="display-md">
            One WebSocket, five stages, streaming end to end
          </motion.h2>
          <div className="pipeline">
            {PIPELINE.map((p) => (
              <motion.div {...fade} key={p.step} className="panel pipeline-step">
                <span className="label-mono">{p.step}</span>
                <strong>{p.label}</strong>
                <span className="mono-id" style={{ fontSize: "0.68rem" }}>
                  {p.note}
                </span>
              </motion.div>
            ))}
          </div>
        </section>

        <section className="container-page section">
          <div className="feature-grid">
            {FEATURES.map((f) => (
              <motion.div {...fade} key={f.title} className="panel feature-card">
                <div className="icon-box">{f.icon}</div>
                <strong>{f.title}</strong>
                <p>{f.body}</p>
              </motion.div>
            ))}
          </div>
        </section>
      </main>

      <footer className="container-page landing-footer">
        <span className="mono-id">Haven · built with FastAPI, Groq Whisper + Orpheus, LiteLLM, Silero VAD, Next.js</span>
        <Link href="/insights" className="mono-id">
          Live insights →
        </Link>
      </footer>
    </div>
  );
}
