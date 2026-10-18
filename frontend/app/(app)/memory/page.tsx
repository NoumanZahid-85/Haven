"use client";

import { useCallback, useEffect, useState } from "react";
import { Brain, Trash, ArrowClockwise } from "@phosphor-icons/react";
import { api } from "@/lib/api";
import { useIdentity } from "@/components/auth";
import { EmptyPanel, ErrorPanel, LoadingRows, PageHeader, SectionHeader } from "@/app/components/ui";

export default function MemoryPage() {
  const { identity } = useIdentity();
  const userId = identity?.customerId ?? "";
  const [facts, setFacts] = useState<string[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [forgetting, setForgetting] = useState(false);

  const fetchFacts = useCallback(
    () =>
      api.memory(userId).then(
        (r) => {
          setError(null);
          setFacts(r.facts);
        },
        (err: unknown) => setError(err instanceof Error ? err.message : String(err)),
      ),
    [userId],
  );
  const load = () => void fetchFacts();

  useEffect(() => {
    if (userId) void fetchFacts();
  }, [userId, fetchFacts]);

  const forget = async () => {
    if (!window.confirm("Erase everything Haven remembers about you? This can't be undone.")) return;
    setForgetting(true);
    try {
      await api.forget(userId);
      setFacts([]);
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setForgetting(false);
    }
  };

  return (
    <div style={{ display: "flex", flexDirection: "column", height: "100dvh", minWidth: 0 }}>
      <PageHeader
        icon={<Brain size={17} weight="duotone" />}
        title="Memory"
        subtitle="What Haven carries between conversations"
        actions={
          <button className="btn-ghost btn-sm" onClick={load}>
            <ArrowClockwise size={13} /> Refresh
          </button>
        }
      />
      <div className="scroll-area" style={{ flex: 1, padding: "24px clamp(16px, 4vw, 40px)" }}>
        <div style={{ maxWidth: 720, margin: "0 auto", display: "grid", gap: 18 }}>
          <p style={{ color: "var(--text-mid)", fontSize: "0.9rem", lineHeight: 1.6 }}>
            After each conversation, Haven saves a few short facts — people in your life, what you&apos;re working
            through, what has helped — so next time you don&apos;t have to start from zero. It&apos;s yours: you can
            see all of it here and erase it at any time.
          </p>

          {error ? (
            <ErrorPanel message={error} onRetry={load} />
          ) : facts === null ? (
            <LoadingRows count={3} />
          ) : facts.length === 0 ? (
            <EmptyPanel title="Nothing remembered yet" message="Have a conversation on the Talk page and check back." />
          ) : (
            <div className="panel" style={{ overflow: "hidden" }}>
              <SectionHeader
                label={`${facts.length} remembered ${facts.length === 1 ? "fact" : "facts"}`}
                right={
                  <button className="btn-danger btn-sm" onClick={() => void forget()} disabled={forgetting}>
                    <Trash size={13} /> Forget everything
                  </button>
                }
              />
              <ul style={{ listStyle: "none" }}>
                {facts.map((f, i) => (
                  <li key={i} className="memory-row">
                    {f}
                  </li>
                ))}
              </ul>
            </div>
          )}
        </div>
      </div>
    </div>
  );
}
