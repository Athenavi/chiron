"""/healthz 与 /readyz（契约 §4）。"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any

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
    """给了外部 DSN 但连不上 → 进程照样起，/readyz 503 且带真实错误。

    **存储初始化是后台 task**（见 app/main.py::lifespan）：TestClient 进入上下文时
    startup 已返回、bootstrap 可能还在跑。这里轮询到 ``storage_ready`` 再断言，
    否则会读到 ``status == "starting"`` 而不是最终的 ``degraded``。
    """
    settings = make_settings(
        tmp_path,
        pg_enabled=True,
        run_migrations=False,
        # 1 端口必然拒绝连接；external 分支只试一次，不会卡 60s
        postgres_dsn="postgresql://chiron:chiron@127.0.0.1:1/chiron",
    )

    with TestClient(create_app(settings)) as client:
        deadline = time.monotonic() + 10
        payload: dict[str, Any] = {}
        while time.monotonic() < deadline:
            response = client.get("/readyz")
            payload = response.json()
            if payload.get("status") != "starting":
                break
            time.sleep(0.05)
        else:
            raise AssertionError(f"存储初始化 10s 内没有结束：{payload}")

    assert response.status_code == 503
    assert payload["status"] == "degraded"
    assert payload["pg"] is False
    assert payload["errors"], "存储失败必须出现在 errors 里"


def test_readyz_stays_ready_when_only_redis_is_down(tmp_path: Path) -> None:
    """**PG 正常、只有 redis 挂了 → /readyz 必须 200**，而不是 503。

    这是本文件最容易被"改回去"的一条断言，理由：

    desktop-local 的业务端点（chat / conversations / agents / media / memory /
    knowledge）对 redis 零引用 —— 全仓只有 ``api/health.py`` 读 ``app.state.redis``。
    redis 是给 python-engine（云端引擎）用的。若让 redis 判死整体就绪，壳层探
    ``/readyz`` 就永远等不到就绪，用户看到的是"应用起不来"，比真实故障更误导。

    所以这里用一个**假 redis**（health 恒 False）配一个**假 pg**（health 恒 True），
    不碰真实存储，只锁住判定逻辑本身。
    """

    class _OkPg:
        backend = "fake"
        dsn = "postgresql://fake/fake"

        async def health_async(self) -> dict[str, Any]:
            return {"ok": True, "backend": "fake", "server_version": "18.6", "error": ""}

        def stop(self) -> None:
            return None

    class _DownRedis:
        backend = "unavailable"
        error = "fakeredis 未安装；内置 redis-server 也不存在"

        async def health(self) -> dict[str, Any]:
            return {"ok": False, "backend": "unavailable", "error": self.error}

        async def stop(self) -> None:
            return None

    settings = make_settings(tmp_path, pg_enabled=True, redis_enabled=True, run_migrations=False)
    with TestClient(create_app(settings)) as client:
        client.app.state.pg = _OkPg()
        client.app.state.redis = _DownRedis()
        # bootstrap 已完成（TestClient 上下文里 storage_ready 由 lifespan 置真；
        # 显式再置一次，避免后台 task 与本用例的猴补丁竞争）
        client.app.state.storage_ready = True
        client.app.state.storage_errors = []
        response = client.get("/readyz")
        payload = response.json()

    assert response.status_code == 200, f"redis 挂了不应让整体 503：{payload}"
    assert payload["pg"] is True
    assert payload["redis"] is False
    # 但**不能假装没事**：状态如实降级，且原因写在 redis_error 里
    assert payload["status"] == "degraded", "redis 缺失必须如实反映在 status 上"
    assert payload["redis_error"], "redis 失败原因必须如实上报"


def test_readyz_stays_unready_when_pg_is_down_even_if_redis_ok(tmp_path: Path) -> None:
    """/readyz 的成败**只看 PG**：PG 挂了哪怕 redis 正常也必须 503。

    与上一条互为对照 —— 证明"降级 redis 判定"没有把 PG 也一起放过。
    """

    class _DownPg:
        backend = "external"
        dsn = "postgresql://127.0.0.1:1/chiron"
        attempts: list[str] = []

        async def health_async(self) -> dict[str, Any]:
            return {"ok": False, "backend": "external", "error": "connection refused"}

        def stop(self) -> None:
            return None

    class _OkRedis:
        backend = "bundled"
        error = ""

        async def health(self) -> dict[str, Any]:
            return {"ok": True, "backend": "bundled", "error": "", "lua": True}

        async def stop(self) -> None:
            return None

    settings = make_settings(tmp_path, pg_enabled=True, redis_enabled=True, run_migrations=False)
    with TestClient(create_app(settings)) as client:
        client.app.state.pg = _DownPg()
        client.app.state.redis = _OkRedis()
        client.app.state.storage_ready = True
        client.app.state.storage_errors = []
        response = client.get("/readyz")
        payload = response.json()

    assert response.status_code == 503, f"PG 挂了必须 503：{payload}"
    assert payload["status"] == "degraded"
    assert payload["pg"] is False
    assert payload["redis"] is True
