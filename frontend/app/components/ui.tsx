// UI kit — the few shared pieces every page uses: a page heading and the
// loading / error / empty trio, so pages compose instead of copy.

"use client";

import type { ReactNode } from "react";

export function PageHeading({ title, lede }: { title: string; lede?: ReactNode }) {
  return (
    <>
      <h1 className="page-title">{title}</h1>
      {lede && <p className="page-lede">{lede}</p>}
    </>
  );
}

export function Loading({ rows = 3 }: { rows?: number }) {
  return (
    <div style={{ display: "grid", gap: 10 }} aria-busy="true" aria-label="Loading">
      {Array.from({ length: rows }).map((_, i) => (
        <div key={i} className="shimmer" />
      ))}
    </div>
  );
}

export function ErrorNotice({ message, onRetry }: { message: string; onRetry?: () => void }) {
  return (
    <div className="notice notice-error" role="alert">
      <p style={{ marginBottom: onRetry ? 12 : 0 }}>Couldn&apos;t reach the server: {message}</p>
      {onRetry && (
        <button className="btn-quiet btn-sm" onClick={onRetry}>
          Try again
        </button>
      )}
    </div>
  );
}

export function EmptyNotice({ children }: { children: ReactNode }) {
  return <div className="notice">{children}</div>;
}
