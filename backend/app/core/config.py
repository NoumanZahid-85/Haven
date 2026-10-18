"""
Centralized settings using pydantic-settings for environment variable management.
Validates env vars at startup — catches missing keys before runtime.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

# Find .env — works whether you run from backend/ or project root
_backend_dir = Path(__file__).resolve().parent.parent.parent  # backend/
_project_root = _backend_dir.parent  # project root

# pydantic-settings accepts a tuple: first match wins
_env_files = tuple(str(p) for p in [_backend_dir / ".env", _project_root / ".env"] if p.exists()) or (".env",)


class Settings(BaseSettings):
    """Application configuration loaded from environment / .env file."""

    model_config = SettingsConfigDict(
        env_file=_env_files,
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # ── Storage ─────────────────────────────────────────────────────
    database_url: str  # postgresql+asyncpg://...  (turns, memories, evals)
    redis_url: str = ""  # short-term conversation window; optional

    # ── Model providers ─────────────────────────────────────────────
    gemini_api_key: str = ""  # Live mode (speech-to-speech), fallback LLM, embeddings
    deepgram_api_key: str = ""  # Pipeline mode: Flux STT + Aura-2 streaming TTS
    groq_api_key: str = ""  # Pipeline-mode + text-chat LLM
    openai_api_key: str = ""
    openai_base_url: str = ""
    openai_model_name: str = ""

    # ── Voice ───────────────────────────────────────────────────────
    live_model: str = "gemini-3.1-flash-live-preview"
    live_voice: str = "Aoede"
    tts_voice: str = "aura-2-thalia-en"

    # ── Conversation ────────────────────────────────────────────────
    memory_turns: int = 10  # short-term window kept per session
    memory_facts: int = 12  # long-term facts injected into the prompt
    knowledge_top_k: int = 2  # coping techniques retrieved per turn

    # ── Application ────────────────────────────────────────────────
    app_name: str = "Haven"
    debug: bool = False
    log_level: str = "INFO"


@lru_cache
def get_settings() -> Settings:
    """Cached singleton — created once, reused for the process lifetime."""
    return Settings()  # type: ignore[call-arg]
