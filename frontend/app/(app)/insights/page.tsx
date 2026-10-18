"use client";

import { useCallback, useEffect, useState } from "react";
import { ChartLineUp, ArrowClockwise } from "@phosphor-icons/react";
import { api } from "@/lib/api";
import type { Insights, Percentiles } from "@/lib/types";
import { ErrorPanel, LoadingRows, PageHeader, SectionHeader } from "@/app/components/ui";
import { formatMs } from "@/lib/format";

const STAGES: { key: keyof Insights["latency_ms"]; label: string; note: string }[] = [
  { key: "stt", label: "Speech-to-text", note: "end of speech → transcript" },
  { key: "llm_first_token", label: "LLM first token", note: "prompt sent → first token" },
  { key: "first_audio", label: "First audio", note: "end of speech → first sound" },
  { key: "full_turn", label: "Full turn", note: "end of speech → last audio sent" },
];

const QUALITY: { key: keyof Insights["quality"]; label: string }[] = [
  { key: "overall", label: "Overall" },
  { key: "empathy", label: "Empathy" },
  { key: "safety", label: "Safety" },
  { key: "helpfulness", label: "Helpfulness" },
  { key: "voice_fit", label: "Voice fit" },
];

function Stat({ value, label }: { value: string | number; label: string }) {
  return (
    <div className="panel" style={{ padding: "16px 18px" }}>
      <div className="stat-number">{value}</div>
      <div className="label-mono" style={{ marginTop: 6 }}>
        {label}
      </div>
    </div>
  );
}

function LatencyRow({ label, note, p }: { label: string; note: string; p: Percentiles }) {
  return (
    <div className="insight-row">
      <div>
        <div style={{ fontWeight: 600, fontSize: "0.9rem" }}>{label}</div>
        <div className="mono-id" style={{ fontSize: "0.66rem" }}>
          {note}
        </div>
      </div>
      <div className="insight-values">
        <span>
          p50 <strong>{formatMs(p.p50)}</strong>
        </span>
        <span>
          p95 <strong>{formatMs(p.p95)}</strong>
        </span>
        <span className="mono-id">n={p.n}</span>
      </div>
    </div>
  );
}

export default function InsightsPage() {
  const [data, setData] = useState<Insights | null>(null);
  const [error, setError] = useState<string | null>(null);

  const fetchInsights = useCallback(
    () =>
      api.insights().then(
        (d) => {
          setError(null);
          setData(d);
        },
        (err: unknown) => setError(err instanceof Error ? err.message : String(err)),
      ),
    [],
  );
  const load = () => void fetchInsights();

  useEffect(() => {
    void fetchInsights();
  }, [fetchInsights]);

  return (
    <div style={{ display: "flex", flexDirection: "column", height: "100dvh", minWidth: 0 }}>
      <PageHeader
        icon={<ChartLineUp size={17} weight="duotone" />}
        title="Insights"
        subtitle="Measured from stored production turns"
        actions={
          <button className="btn-ghost btn-sm" onClick={load}>
            <ArrowClockwise size={13} /> Refresh
          </button>
        }
      />
      <div className="scroll-area" style={{ flex: 1, padding: "24px clamp(16px, 4vw, 40px)" }}>
        <div style={{ maxWidth: 900, margin: "0 auto", display: "grid", gap: 20 }}>
          {error ? (
            <ErrorPanel message={error} onRetry={load} />
          ) : !data ? (
            <LoadingRows count={5} />
          ) : (
            <>
              <div className="stat-grid">
                <Stat value={data.volume.turns} label="Turns" />
                <Stat value={data.volume.conversations} label="Conversations" />
                <Stat value={data.volume.users} label="People" />
                <Stat value={data.volume.turns_24h} label="Turns · 24h" />
                <Stat value={data.volume.barge_ins} label="Barge-ins" />
              </div>

              <div className="panel" style={{ overflow: "hidden" }}>
                <SectionHeader label="Voice latency (most recent 1,000 voice turns)" />
                {STAGES.map((s) => (
                  <LatencyRow key={s.key} label={s.label} note={s.note} p={data.latency_ms[s.key]} />
                ))}
              </div>

              <div className="two-col">
                <div className="panel" style={{ overflow: "hidden" }}>
                  <SectionHeader label="Safety tiers" />
                  {(["none", "elevated", "crisis"] as const).map((r) => (
                    <div key={r} className="insight-row">
                      <span className={`risk-dot risk-${r}`}>{r}</span>
                      <strong>{data.safety[r] ?? 0}</strong>
                    </div>
                  ))}
                </div>
                <div className="panel" style={{ overflow: "hidden" }}>
                  <SectionHeader label={`LLM judge · ${data.quality.evaluated_turns} turns scored (1–5)`} />
                  {QUALITY.map((q) => (
                    <div key={q.key} className="insight-row">
                      <span>{q.label}</span>
                      <strong>{data.quality[q.key] ?? "—"}</strong>
                    </div>
                  ))}
                </div>
              </div>
              <p className="mono-id" style={{ fontSize: "0.7rem", lineHeight: 1.6 }}>
                Scores are written by <code>python -m scripts.evaluate production</code>; low scorers are promoted into
                the regression suite that gates prompt and guardrail changes.
              </p>
            </>
          )}
        </div>
      </div>
    </div>
  );
}
