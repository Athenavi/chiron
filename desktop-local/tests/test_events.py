"""/events 的 SSE 心跳（契约 §4：第一批只发心跳）。"""

from __future__ import annotations

from app.config import Settings
from app.main import create_app
from fastapi.testclient import TestClient


def test_events_streams_heartbeats(settings: Settings) -> None:
    settings.sse_max_heartbeats = 2
    with TestClient(create_app(settings)) as client:
        response = client.get("/events", params={"session_id": "sess-1"})

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/event-stream")
    body = response.text
    assert body.count("event: heartbeat") == 2
    assert '"session_id": "sess-1"' in body
    assert '"seq": 1' in body and '"seq": 2' in body


def test_events_without_session_id_still_works(settings: Settings) -> None:
    settings.sse_max_heartbeats = 1
    with TestClient(create_app(settings)) as client:
        response = client.get("/events")

    assert response.status_code == 200
    assert response.text.count("event: heartbeat") == 1
