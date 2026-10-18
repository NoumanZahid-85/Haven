"use client";

import { useCallback, useEffect, useState } from "react";
import Link from "next/link";
import { api } from "@/lib/api";
import { useIdentity } from "@/components/auth";
import { EmptyNotice, ErrorNotice, Loading, PageHeading } from "@/app/components/ui";

export default function MemoryPage() {
  const { identity } = useIdentity();
  const userId = identity?.customerId ?? "";
  const [facts, setFacts] = useState<string[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [erasing, setErasing] = useState(false);

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

  useEffect(() => {
    if (userId) void fetchFacts();
  }, [userId, fetchFacts]);

  const erase = async () => {
    if (!window.confirm("Erase everything Haven remembers about you, including your saved wins? This can't be undone.")) return;
    setErasing(true);
    try {
      await api.forget(userId);
      setFacts([]);
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setErasing(false);
    }
  };

  return (
    <div className="page">
      <PageHeading
        title="What Haven remembers"
        lede="After you talk, Haven keeps a few short notes, like the people in your life or what has helped before, so next time you don't start from zero. Only you can see them, and you can erase them anytime."
      />

      {error ? (
        <ErrorNotice message={error} onRetry={() => void fetchFacts()} />
      ) : facts === null ? (
        <Loading />
      ) : facts.length === 0 ? (
        <EmptyNotice>
          <strong>Nothing yet.</strong> Have a conversation on <Link href="/talk">Talk</Link> and your notes will appear
          here.
        </EmptyNotice>
      ) : (
        <>
          <div className="row-between">
            <p style={{ color: "var(--haze)" }}>
              {facts.length} {facts.length === 1 ? "note" : "notes"}
            </p>
            <button className="btn-danger btn-sm" onClick={() => void erase()} disabled={erasing}>
              {erasing ? "Erasing…" : "Erase all notes"}
            </button>
          </div>
          <ul className="fact-list">
            {facts.map((f, i) => (
              <li key={i}>{f}</li>
            ))}
          </ul>
        </>
      )}
    </div>
  );
}
