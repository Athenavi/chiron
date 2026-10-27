"""本地 core 的 Bearer 设备 token 鉴权（契约 §4）。

两层都要测到，缺一层就等于没测：

- ``auth_enabled=False``（默认）时**必须**放行一切 —— 现有握手 / 前端 / 测试全都建立在
  "本地后端不校验 token" 之上，这里一旦回归，表现是全场 401 而不仅是某个用例红；
- ``auth_enabled=True`` 时只放行三个免鉴权端点 + 带对 token 的请求，其余一律 401。
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
from app.config import Settings
from app.main import create_app
from app.middleware import EXEMPT_PATHS, parse_bearer, tokens_match
from fastapi.testclient import TestClient

from tests.conftest import make_settings

#: 测试里显式下发的设备 token（``load_or_create_token`` 会优先用 CHIRON_DEVICE_TOKEN）
TOKEN = "test-device-token-0123456789abcdef"


@pytest.fixture
def device_token(monkeypatch: pytest.MonkeyPatch) -> str:
    """固定设备 token，避免测试读到上一次跑残留的 ``<data-dir>/auth/device-token``。"""
    monkeypatch.setenv("CHIRON_DEVICE_TOKEN", TOKEN)
    return TOKEN


@pytest.fixture
def local_settings(tmp_path: Path) -> Settings:
    """默认配置（auth 关闭）的本地设置。"""
    return make_settings(tmp_path)


@pytest.fixture
def open_client(local_settings: Settings) -> Iterator[TestClient]:
    with TestClient(create_app(local_settings)) as test_client:
        yield test_client


@pytest.fixture
def guarded_client(tmp_path: Path, device_token: str) -> Iterator[TestClient]:
    """打开了设备 token 鉴权的应用。"""
    settings = make_settings(tmp_path, auth_enabled=True)
    with TestClient(create_app(settings)) as test_client:
        yield test_client


# ------------------------------------------------------------------ 开关与配置
def test_auth_disabled_by_default(local_settings: Settings) -> None:
    assert local_settings.auth_enabled is False
    assert local_settings.auth_mode() == "disabled"


def test_auth_enabled_from_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """桌面端只会设环境变量，所以 env → 字段这条链本身要验一遍。"""
    monkeypatch.setenv("CHIRON_LOCAL_AUTH_ENABLED", "1")
    settings = make_settings(tmp_path)
    assert settings.auth_enabled is True
    assert settings.auth_mode() == "device_token"


def test_readyz_reports_device_token_mode(guarded_client: TestClient) -> None:
    payload = guarded_client.get("/readyz").json()
    assert payload["auth"] == "device_token"


# ------------------------------------------------------------------ 关闭时放行
def test_disabled_lets_everything_through(open_client: TestClient) -> None:
    assert open_client.get("/v1/models").status_code == 200
    assert open_client.get("/v1/kb").status_code in (200, 403)  # 知识库另有开关，只要求不是 401


def test_disabled_ignores_bogus_token(open_client: TestClient) -> None:
    """关着的时候送错 token 也不能 401：第一批的前提就是"完全不看这个头"。"""
    response = open_client.get("/v1/models", headers={"Authorization": "Bearer nope"})
    assert response.status_code == 200


# ------------------------------------------------------------------ 开启时校验
def test_missing_token_is_401(guarded_client: TestClient) -> None:
    response = guarded_client.get("/v1/models")
    assert response.status_code == 401
    body = response.json()
    assert body["error"] == "unauthorized"
    assert response.headers["www-authenticate"] == "Bearer"


def test_wrong_token_is_401(guarded_client: TestClient) -> None:
    response = guarded_client.get("/v1/models", headers={"Authorization": "Bearer wrong-token"})
    assert response.status_code == 401


def test_non_bearer_scheme_is_401(guarded_client: TestClient, device_token: str) -> None:
    """``Basic`` 里塞对的值也不行：scheme 不对就当没给。"""
    response = guarded_client.get("/v1/models", headers={"Authorization": f"Basic {device_token}"})
    assert response.status_code == 401


def test_correct_token_is_200(guarded_client: TestClient, device_token: str) -> None:
    response = guarded_client.get("/v1/models", headers={"Authorization": f"Bearer {device_token}"})
    assert response.status_code == 200
    assert response.json() == {"data": []}


def test_exempt_paths_need_no_token(guarded_client: TestClient) -> None:
    assert guarded_client.get("/healthz").status_code == 200
    assert guarded_client.get("/readyz").status_code == 200
    # 换 token 的入口必须公开，否则"要 token 才能拿 token"是死循环
    session = guarded_client.post("/v1/auth/session")
    assert session.status_code == 200
    assert session.json()["data"]["token"]


def test_session_token_matches_guarded_token(guarded_client: TestClient, device_token: str) -> None:
    """``/v1/auth/session`` 发出去的 token 就是中间件认的那一个（否则前端必 401）。"""
    issued = guarded_client.post("/v1/auth/session").json()["data"]["token"]
    assert issued == device_token
    response = guarded_client.get("/v1/models", headers={"Authorization": f"Bearer {issued}"})
    assert response.status_code == 200


def test_exempt_paths_tolerate_trailing_slash(guarded_client: TestClient) -> None:
    """中间件先于路由执行，看不到 FastAPI 的 307，得自己容忍尾斜杠。"""
    assert guarded_client.get("/healthz/").status_code == 200


def test_exempt_paths_cover_contract() -> None:
    assert EXEMPT_PATHS == {"/healthz", "/readyz", "/v1/auth/session"}


def test_sse_stream_passes_with_token(guarded_client: TestClient, device_token: str) -> None:
    """纯 ASGI 中间件不应缓冲 SSE：带 token 的心跳必须照常流出来。"""
    response = guarded_client.get(
        "/events",
        params={"session_id": "s1"},
        headers={"Authorization": f"Bearer {device_token}"},
    )
    assert response.status_code == 200
    assert "event: heartbeat" in response.text


def test_sse_without_token_is_401(guarded_client: TestClient) -> None:
    assert guarded_client.get("/events", params={"session_id": "s1"}).status_code == 401


# ------------------------------------------------------------------ 纯函数
@pytest.mark.parametrize(
    ("header", "expected"),
    [
        (None, ""),
        ("", ""),
        ("Bearer", ""),
        ("Bearer ", ""),
        ("bearer abc", "abc"),
        ("Bearer abc", "abc"),
        ("Basic abc", ""),
    ],
)
def test_parse_bearer(header: str | None, expected: str) -> None:
    assert parse_bearer(header) == expected


def test_tokens_match_rejects_empty() -> None:
    """两个空串绝不能相等 —— 否则没配 token 时鉴权等于没开。"""
    assert tokens_match("", "") is False
    assert tokens_match(TOKEN, "") is False
    assert tokens_match("", TOKEN) is False
    assert tokens_match(TOKEN, TOKEN) is True
    assert tokens_match(TOKEN, TOKEN + "x") is False
