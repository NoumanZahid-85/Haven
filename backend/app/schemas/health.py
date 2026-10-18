"""Health check response schema."""

from __future__ import annotations

from pydantic import BaseModel


class HealthResponse(BaseModel):
    status: str
    db: str
    redis: str
    live: str
    pipeline: str
    knowledge: str
