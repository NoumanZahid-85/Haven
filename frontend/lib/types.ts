// Central API types — one definition per backend contract.
// The FastAPI response schemas live in backend/app/; keep these in sync.

export type Risk = "none" | "elevated" | "crisis";

/** live: Gemini native speech-to-speech. pipeline: Deepgram Flux → Groq → Deepgram Aura-2. */
export type VoiceMode = "live" | "pipeline";

export interface ChatResponse {
  reply: string;
  risk: Risk;
  techniques: string[];
  llm_first_token_ms: number | null;
}

export interface HealthData {
  status: "ok" | "degraded";
  db: string;
  redis: string;
  live: string;
  pipeline: string;
  knowledge: string;
}

export interface MemoryResponse {
  facts: string[];
}

export interface Percentiles {
  p50: number | null;
  p95: number | null;
  n: number;
}

export interface ModeLatency {
  end_of_turn: Percentiles;
  llm_first_token: Percentiles;
  first_audio: Percentiles;
  full_turn: Percentiles;
}

export interface Insights {
  volume: { turns: number; conversations: number; users: number; turns_24h: number; barge_ins: number };
  safety: Partial<Record<Risk, number>>;
  latency_ms: Record<VoiceMode, ModeLatency>;
  quality: {
    evaluated_turns: number;
    overall: number | null;
    empathy: number | null;
    safety: number | null;
    helpfulness: number | null;
    voice_fit: number | null;
  };
}

/** Per-turn latency the voice socket reports after each reply (ms). */
export interface TurnMetrics {
  stt_ms: number | null;
  llm_first_token_ms: number | null;
  first_audio_ms: number | null;
  total_ms: number | null;
}
