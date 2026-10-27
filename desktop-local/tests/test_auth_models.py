"""/v1/auth/session 与 /v1/models（契约 §4）。"""

from __future__ import annotations

from app.api.auth import LOCAL_USER
from app.config import Settings
from fastapi.testclient import TestClient


def test_session_returns_local_owner(client: TestClient) -> None:
    response = client.post("/v1/auth/session")
    assert response.status_code == 200
    body = response.json()
    assert set(body) == {"data"}
    data = body["data"]
    assert data["user"] == LOCAL_USER
    assert data["user"]["role"] == "owner"
    assert data["user"]["tenant_id"] == 1
    assert isinstance(data["token"], str)
    assert len(data["token"]) >= 20


def test_session_token_is_stable_across_calls(client: TestClient, settings: Settings) -> None:
    first = client.post("/v1/auth/session").json()["data"]["token"]
    second = client.post("/v1/auth/session").json()["data"]["token"]
    assert first == second
    # 落在 <data-dir>/auth/device-token，重启后还能复用
    assert settings.device_token_path.read_text(encoding="utf-8").strip() == first


def test_models_is_empty_placeholder(client: TestClient) -> None:
    response = client.get("/v1/models")
    assert response.status_code == 200
    assert response.json() == {"data": []}
