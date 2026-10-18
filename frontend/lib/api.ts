// The single data seam between the frontend and the FastAPI backend.
// Pages name the data they want; this module owns URL construction,
// HTTP error handling, timeouts, and JSON parsing.

import type { ChatResponse, HealthData, Insights, Journey, Lang, MemoryResponse, Takeaway, VoiceMode } from "./types";

const BACKEND = process.env.NEXT_PUBLIC_BACKEND_URL ?? "http://localhost:8000";

export class ApiError extends Error {
  readonly status: number;
  constructor(message: string, status: number) {
    super(message);
    this.name = "ApiError";
    this.status = status;
  }
}

const COLD_START_RETRY_DELAYS_MS = [4_000, 10_000]; // total ~14s of backoff across 2 retries

function isNetworkFailure(err: unknown): boolean {
  // A plain "Failed to fetch" / connection-refused error (not an HTTP error
  // status, which throws ApiError instead) — the signature of hitting a
  // Render free-tier instance that's currently spun down and waking up.
  return err instanceof TypeError || (err instanceof DOMException && err.name === "AbortError");
}

async function fetchJSON<T>(
  path: string,
  init?: RequestInit,
  timeoutMs = 45_000,
  retry = true
): Promise<T> {
  let lastErr: unknown;
  const retryDelays = retry ? COLD_START_RETRY_DELAYS_MS : [];
  for (let attempt = 0; attempt <= retryDelays.length; attempt++) {
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), timeoutMs);
    try {
      const res = await fetch(`${BACKEND}${path}`, { ...init, signal: controller.signal });
      if (!res.ok) {
        let detail = `HTTP ${res.status}`;
        try {
          const body = await res.json();
          if (body?.detail) detail = typeof body.detail === "string" ? body.detail : detail;
        } catch {
          /* non-JSON error body */
        }
        throw new ApiError(detail, res.status);
      }
      return (await res.json()) as T;
    } catch (err) {
      lastErr = err;
      if (isNetworkFailure(err) && attempt < retryDelays.length) {
        await new Promise((r) => setTimeout(r, retryDelays[attempt]));
        continue;
      }
      if (isNetworkFailure(err) && retry) {
        throw new Error(
          "The server is waking up from being idle (free hosting spins down after inactivity) — please try again in a few seconds."
        );
      }
      throw err;
    } finally {
      clearTimeout(timer);
    }
  }
  throw lastErr;
}

/** Best-effort call for endpoints with no (or unreliable) response body. */
async function send(path: string, init?: RequestInit): Promise<void> {
  for (let attempt = 0; attempt <= COLD_START_RETRY_DELAYS_MS.length; attempt++) {
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), 20_000);
    try {
      const res = await fetch(`${BACKEND}${path}`, { ...init, signal: controller.signal });
      if (!res.ok) throw new ApiError(`HTTP ${res.status}`, res.status);
      return;
    } catch (err) {
      if (isNetworkFailure(err) && attempt < COLD_START_RETRY_DELAYS_MS.length) {
        await new Promise((r) => setTimeout(r, COLD_START_RETRY_DELAYS_MS[attempt]));
        continue;
      }
      throw err;
    } finally {
      clearTimeout(timer);
    }
  }
}

export const api = {
  health: () => fetchJSON<HealthData>("/health", undefined, 15_000, true), // retry once — cold-start can take ~14s

  chat: (message: string, sessionId: string, userId: string, lang: Lang) =>
    fetchJSON<ChatResponse>("/api/v1/chat", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ message, session_id: sessionId, user_id: userId, lang }),
    }),

  takeaway: (sessionId: string, userId: string, lang: Lang) =>
    fetchJSON<Takeaway>(
      `/api/v1/sessions/${encodeURIComponent(sessionId)}/takeaway?${new URLSearchParams({ user_id: userId, lang })}`,
      { method: "POST" },
      45_000,
      false,
    ),

  journey: (userId: string) => fetchJSON<Journey>(`/api/v1/journey/${encodeURIComponent(userId)}`),

  memory: (userId: string) => fetchJSON<MemoryResponse>(`/api/v1/memory/${encodeURIComponent(userId)}`),

  forget: (userId: string) => send(`/api/v1/memory/${encodeURIComponent(userId)}`, { method: "DELETE" }),

  insights: () => fetchJSON<Insights>("/api/v1/insights", undefined, 20_000),

  voice: {
    wsUrl: (mode: VoiceMode, lang: Lang, sessionId: string, userId: string, bargeIn: boolean) => {
      const wsBase = BACKEND.replace(/^http/, "ws");
      const params = new URLSearchParams({ mode, lang, session_id: sessionId, barge_in: bargeIn ? "1" : "0" });
      if (userId) params.set("user_id", userId);
      return `${wsBase}/api/v1/voice/ws?${params}`;
    },
  },
};
