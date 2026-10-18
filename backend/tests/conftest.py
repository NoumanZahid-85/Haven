"""Pytest configuration and shared fixtures."""

from __future__ import annotations

import os

# Set BEFORE importing app modules: a throwaway SQLite DB and no provider keys,
# so the suite never touches real infrastructure or makes network calls.
os.environ["DATABASE_URL"] = "sqlite+aiosqlite:///test.db"
for key in ("REDIS_URL", "GROQ_API_KEY", "GEMINI_API_KEY", "OPENAI_API_KEY"):
    os.environ[key] = ""
os.environ.setdefault("DEBUG", "false")
