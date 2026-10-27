"""测试夹具。

单元测试**不碰真实存储**（PG / Redis 全关），只验证骨架的契约形状；
真实存储能力由 ``scripts/verify_p0.py`` 实测，这里不重复也不假装。
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
from app.config import Settings
from app.main import create_app
from fastapi.testclient import TestClient


def make_settings(tmp_path: Path, **overrides: object) -> Settings:
    defaults: dict[str, object] = {
        "data_dir": tmp_path / "data",
        "pg_enabled": False,
        "redis_enabled": False,
        # 心跳间隔压到 10ms + 只发 2 条，让 SSE 测试不用等 15s
        "sse_heartbeat_seconds": 0.01,
        "sse_max_heartbeats": 2,
    }
    defaults.update(overrides)
    return Settings(**defaults)


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return make_settings(tmp_path)


@pytest.fixture
def client(settings: Settings) -> Iterator[TestClient]:
    with TestClient(create_app(settings)) as test_client:
        yield test_client
