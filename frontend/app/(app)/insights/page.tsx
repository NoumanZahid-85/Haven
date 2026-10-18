"use client";

import { useCallback, useEffect, useState } from "react";
import { api } from "@/lib/api";
import type { Insights, ModeLatency, VoiceMode } from "@/lib/types";
import { ErrorNotice, Loading, PageHeading } from "@/app/components/ui";
import { formatMs } from "@/lib/format";

const STAGES: { key: keyof ModeLatency; label: string }[] = [
  { key: "end_of_turn", label: "Noticing you finished speaking" },
  { key: "llm_first_token", label: "First word written by the model" },
  { key: "first_audio", label: "First sound you hear" },
  { key: "full_turn", label: "Whole reply delivered" },
];

const MODE_NAMES: Record<VoiceMode, string> = { live: "Natural", pipeline: "Checked" };

const QUALITY: { key: keyof Insights["quality"]; label: string }[] = [
  { key: "empathy", label: "Empathy" },
  { key: "safety", label: "Safety" },
  { key: "helpfulness", label: "Helpfulness" },
  { key: "voice_fit", label: "Sounds right spoken aloud" },
];

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

  useEffect(() => {
    void fetchInsights();
  }, [fetchInsights]);

  return (
    <div className="page">
      <PageHeading
        title="How Haven is doing"
        lede="Measured from real conversations: how fast Haven answers in each mode, how often the safety layer steps in, and how an AI reviewer scores the replies."
      />

      {error ? (
        <ErrorNotice message={error} onRetry={() => void fetchInsights()} />
      ) : !data ? (
        <Loading rows={5} />
      ) : (
        <>
          <div className="big-figures">
            <div>
              <strong>{data.volume.conversations}</strong>
              <span>conversations</span>
            </div>
            <div>
              <strong>{data.volume.turns}</strong>
              <span>replies</span>
            </div>
            <div>
              <strong>{data.volume.turns_24h}</strong>
              <span>replies today</span>
            </div>
            <div>
              <strong>{data.volume.barge_ins}</strong>
              <span>times someone interrupted</span>
            </div>
          </div>

          <h2 className="section-title">Response time</h2>
          <p className="footnote" style={{ marginBottom: 12 }}>
            Timed from the moment you stop speaking. The typical case is the median; the slow case is the 95th
            percentile.
          </p>
          <div className="table-scroll">
            <table className="metric-table">
              <thead>
                <tr>
                  <th scope="col">Stage</th>
                  {(Object.keys(MODE_NAMES) as VoiceMode[]).map((m) => (
                    <th key={m} scope="col" className="num">
                      {MODE_NAMES[m]}: typical / slow
                    </th>
                  ))}
                </tr>
              </thead>
              <tbody>
                {STAGES.map((s) => (
                  <tr key={s.key}>
                    <td>{s.label}</td>
                    {(Object.keys(MODE_NAMES) as VoiceMode[]).map((m) => {
                      const p = data.latency_ms[m]?.[s.key];
                      return (
                        <td key={m} className="num">
                          {p && p.n ? `${formatMs(p.p50)} / ${formatMs(p.p95)}` : "Not measured"}
                        </td>
                      );
                    })}
                  </tr>
                ))}
              </tbody>
            </table>
          </div>

          <h2 className="section-title">Safety layer</h2>
          <table className="metric-table">
            <tbody>
              <tr>
                <td>
                  <span className="risk-dot risk-none">Everyday conversation</span>
                </td>
                <td className="num">{data.safety.none ?? 0}</td>
              </tr>
              <tr>
                <td>
                  <span className="risk-dot risk-elevated">Signs of distress (Haven slowed down and checked in)</span>
                </td>
                <td className="num">{data.safety.elevated ?? 0}</td>
              </tr>
              <tr>
                <td>
                  <span className="risk-dot risk-crisis">Crisis (helplines given, model bypassed)</span>
                </td>
                <td className="num">{data.safety.crisis ?? 0}</td>
              </tr>
            </tbody>
          </table>

          <h2 className="section-title">Reply quality</h2>
          {data.quality.evaluated_turns === 0 ? (
            <p className="footnote">
              No replies reviewed yet. Run <code>python -m scripts.evaluate production</code> to score recent ones.
            </p>
          ) : (
            <>
              <p className="footnote" style={{ marginBottom: 12 }}>
                {data.quality.evaluated_turns} replies scored from 1 to 5 by an AI reviewer. Overall average{" "}
                <strong style={{ color: "var(--moon)" }}>{data.quality.overall}</strong>.
              </p>
              <table className="metric-table">
                <tbody>
                  {QUALITY.map((q) => (
                    <tr key={q.key}>
                      <td>{q.label}</td>
                      <td className="num">{data.quality[q.key] ?? "Not scored"}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </>
          )}
          <p className="footnote">
            The lowest-scoring replies become test cases that every future change to Haven&apos;s instructions has to
            pass.
          </p>
        </>
      )}
    </div>
  );
}
