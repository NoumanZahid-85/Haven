"""HTTP surface: health, text chat, memory, insights — against a SQLite DB."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.services import companion


@pytest.fixture()
def client():
    with TestClient(app) as c:  # runs lifespan → creates tables
        yield c


def test_health_reports_components(client):
    resp = client.get("/health")
    assert resp.status_code == 200
    data = resp.json()
    assert data["db"] == "connected"
    assert data["status"] in ("ok", "degraded")
    assert set(data) == {"status", "db", "redis", "live", "pipeline", "knowledge"}


def test_chat_turn_is_persisted_and_shows_in_insights(client, monkeypatch):
    async def fake_stream(_messages):
        yield "That sounds like a lot. "
        yield "What's weighing on you most?"

    monkeypatch.setattr(companion, "llm_stream", fake_stream)
    before = client.get("/api/v1/insights").json()["volume"]["turns"]

    resp = client.post("/api/v1/chat", json={"message": "work has been overwhelming", "session_id": "s1"})
    assert resp.status_code == 200
    assert resp.json()["reply"] == "That sounds like a lot. What's weighing on you most?"
    assert resp.json()["risk"] == "none"

    insights = client.get("/api/v1/insights").json()
    assert insights["volume"]["turns"] == before + 1
    assert set(insights["latency_ms"]) == {"live", "pipeline"}
    assert set(insights["latency_ms"]["live"]) == {"end_of_turn", "llm_first_token", "first_audio", "full_turn"}


def test_crisis_chat_returns_helpline(client):
    resp = client.post("/api/v1/chat", json={"message": "I want to kill myself", "session_id": "s2"})
    assert resp.json()["risk"] == "crisis"
    assert "988" in resp.json()["reply"]


def test_memory_can_be_read_and_forgotten(client):
    assert client.get("/api/v1/memory/nobody").json() == {"facts": []}
    assert client.delete("/api/v1/memory/nobody").json() == {"deleted": 0}


def test_chat_rejects_empty_message(client):
    assert client.post("/api/v1/chat", json={"message": "", "session_id": "s"}).status_code == 422
