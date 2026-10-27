"""/healthz 与 /readyz（契约 §4）。"""

from __future__ import annotations

from pathlib import Path

from app.main import create_app
from fastapi.testclient import TestClient

from tests.conftest import make_settings


def test_healthz_does_not_touch_storage(client: TestClient) -> None:
    response = client.get("/healthz")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_readyz_shape_when_storage_disabled(client: TestClient) -> None:
    response = client.get("/readyz")
    assert response.status_code == 200
    payload = response.json()
    assert payload["status"] == "ok"
    # 关掉的存储如实报 false，但不算"未就绪"
    assert payload["pg"] is False
    assert payload["redis"] is False
    assert payload["pg_enabled"] is False
    assert payload["redis_enabled"] is False
    # 契约 §4：第一批 /readyz 必须报 auth
    assert payload["auth"] == "disabled"


def test_readyz_reports_degraded_when_pg_unreachable(tmp_path: Path) -> None:
    """给了外部 DSN 但连不上 → 进程照样起，/readyz 503 且带真实错误。"""
    settings = make_settings(
        tmp_path,
        pg_enabled=True,
        run_migrations=False,
        # 1 端口必然拒绝连接；external 分支只试一次，不会卡 60s
        postgres_dsn="postgresql://chiron:chiron@127.0.0.1:1/chiron",
    )

    with TestClient(create_app(settings)) as client:
        response = client.get("/readyz")

    assert response.status_code == 503
    payload = response.json()
    assert payload["status"] == "degraded"
    assert payload["pg"] is False
    assert payload["errors"], "存储失败必须出现在 errors 里"
