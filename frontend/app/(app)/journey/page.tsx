"use client";

import { useCallback, useEffect, useState } from "react";
import Link from "next/link";
import { api } from "@/lib/api";
import { useIdentity } from "@/components/auth";
import type { Journey } from "@/lib/types";
import { EmptyNotice, ErrorNotice, Loading, PageHeading } from "@/app/components/ui";

function when(iso: string | null): string {
  if (!iso) return "";
  return new Date(iso).toLocaleDateString([], { weekday: "short", day: "numeric", month: "short" });
}

export default function JourneyPage() {
  const { identity } = useIdentity();
  const userId = identity?.customerId ?? "";
  const [data, setData] = useState<Journey | null>(null);
  const [error, setError] = useState<string | null>(null);

  const fetchJourney = useCallback(
    () =>
      api.journey(userId).then(
        (d) => {
          setError(null);
          setData(d);
        },
        (err: unknown) => setError(err instanceof Error ? err.message : String(err)),
      ),
    [userId],
  );

  useEffect(() => {
    if (userId) void fetchJourney();
  }, [userId, fetchJourney]);

  return (
    <div className="page">
      <PageHeading
        title="Your journey"
        lede="The wins you mentioned, even the ones you brushed off, and what you took away from each conversation."
      />

      {error ? (
        <ErrorNotice message={error} onRetry={() => void fetchJourney()} />
      ) : !data ? (
        <Loading rows={4} />
      ) : data.wins.length === 0 && data.takeaways.length === 0 ? (
        <EmptyNotice>
          <strong>Nothing here yet.</strong> Have a conversation on <Link href="/talk">Talk</Link>. When you hang up, a
          short takeaway lands here, along with any wins you mention.
        </EmptyNotice>
      ) : (
        <>
          <h2 className="section-title">Wins</h2>
          {data.wins.length === 0 ? (
            <p className="footnote" style={{ marginBottom: 40 }}>
              None yet. Tell Haven about something you did, even something small.
            </p>
          ) : (
            <>
              <p className="footnote" style={{ marginBottom: 14 }}>
                It&apos;s easy to move straight on to the next thing. These happened.
              </p>
              <ul className="wins-list">
                {data.wins.map((w, i) => (
                  <li key={i}>
                    {w.text}
                    <time>{when(w.created_at)}</time>
                  </li>
                ))}
              </ul>
            </>
          )}

          <h2 className="section-title">Conversations</h2>
          {data.takeaways.length === 0 ? (
            <p className="footnote">Takeaways appear here after you end a voice conversation.</p>
          ) : (
            <ul className="card-list">
              {data.takeaways.map((t) => (
                <li key={t.session_id}>
                  <p>{t.summary}</p>
                  {t.mood_start && t.mood_end && (
                    <p className="mood">
                      Came in {t.mood_start}, left {t.mood_end}
                    </p>
                  )}
                  {t.next_step && <p className="step">Next step: {t.next_step}</p>}
                  <time>{when(t.created_at)}</time>
                </li>
              ))}
            </ul>
          )}
        </>
      )}
    </div>
  );
}
