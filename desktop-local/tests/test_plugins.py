"""本地插件端点（占位实现）的测试。

这个占位只有两种出错方式，两种都算有害，所以两边都要钉住：

1. **撒谎**：没有运行时却返回"安装成功 / 已授权 / 测试通过"。断言是 ——
   写操作一律 501，失败之后列表**仍然**是空的；
2. **误报错**：插件页打 ``/v1/plugins`` 拿到 4xx/5xx，于是渲染成"加载失败"。
   断言是 —— 200 + 空列表 + ``status.reason`` 明确写着"未实现"。

鉴权层覆盖两态：默认关闭（现有握手 / 前端全建立在"本地后端不校验 token"之上）和
显式开启（新端点不是豁免路径，这里不额外开一个口子）。
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
from app.api import plugins as plugins_api
from app.main import create_app
from app.middleware import EXEMPT_PATHS
from fastapi.testclient import TestClient

from tests.conftest import make_settings

#: 测试里显式下发的设备 token（``load_or_create_token`` 优先读 CHIRON_DEVICE_TOKEN）
TOKEN = "test-device-token-plugins-0001"


@pytest.fixture
def device_token(monkeypatch: pytest.MonkeyPatch) -> str:
    monkeypatch.setenv("CHIRON_DEVICE_TOKEN", TOKEN)
    return TOKEN


@pytest.fixture
def guarded_client(tmp_path: Path, device_token: str) -> Iterator[TestClient]:
    """打开了设备 token 鉴权的应用（``client`` 夹具则是默认的关闭态）。"""
    settings = make_settings(tmp_path, auth_enabled=True)
    with TestClient(create_app(settings)) as test_client:
        yield test_client


# ------------------------------------------------------------------ 路由形状
def test_router_declares_only_placeholder_routes() -> None:
    """本模块只声明这些端点 —— 多一个"看起来能用"的路径都是误导。"""
    declared = {
        (route.path, method) for route in plugins_api.router.routes for method in (route.methods or set())
    }
    assert declared == {
        ("/v1/plugins", "GET"),
        ("/v1/plugins/status", "GET"),
        ("/v1/plugins/authorization", "GET"),
        ("/v1/plugins/authorization", "PUT"),
        ("/v1/plugins/{name}/approve", "POST"),
        ("/v1/plugins/{name}/install", "POST"),
        ("/v1/plugins/{name}/test", "POST"),
    }


# ------------------------------------------------------------------ 列表 / 状态
def test_list_plugins_is_empty_and_200(client: TestClient) -> None:
    """前端 ``loadPlugins`` 只认 ``res.data.data``；200 + 空数组 = "暂无插件"。"""
    response = client.get("/v1/plugins")
    assert response.status_code == 200
    assert response.json() == {"data": []}


def test_status_reports_not_implemented(client: TestClient) -> None:
    response = client.get("/v1/plugins/status")
    assert response.status_code == 200
    assert response.json() == {
        "data": {"enabled": False, "reason": "local_plugins_not_implemented"},
    }


def test_status_reason_is_the_same_code_as_write_errors(client: TestClient) -> None:
    """状态里的 ``reason`` 与写操作 501 的 ``error`` 是同一个码，前端只需认一次。"""
    reason = client.get("/v1/plugins/status").json()["data"]["reason"]
    error = client.post("/v1/plugins/demo/install", json={}).json()["error"]
    assert reason == error == plugins_api.NOT_IMPLEMENTED


# ------------------------------------------------------------------ 写操作一律 501
@pytest.mark.parametrize("action", ["approve", "install", "test"])
def test_write_actions_are_501(client: TestClient, action: str) -> None:
    response = client.post(f"/v1/plugins/demo/{action}", json={"approved": True})
    assert response.status_code == 501
    assert response.json() == {"error": "local_plugins_not_implemented"}


def test_write_actions_do_not_create_plugins(client: TestClient) -> None:
    """"不伪造已安装"：501 之后列表还是空的，``approve`` 之后也没有 approved 记录。"""
    client.post("/v1/plugins/demo/install", json={"name": "demo", "command": "echo hi"})
    client.post("/v1/plugins/demo/approve", json={"approved": True})
    client.post("/v1/plugins/demo/test")
    assert client.get("/v1/plugins").json() == {"data": []}


# ------------------------------------------------------------------ 用户级授权
def test_authorization_defaults_to_disabled(client: TestClient) -> None:
    """默认关：没被用户确认过就不允许执行命令（与 Go 侧语义一致）。"""
    response = client.get("/v1/plugins/authorization")
    assert response.status_code == 200
    assert response.json() == {"data": {"commands_enabled": False}}


def test_authorization_roundtrip(client: TestClient) -> None:
    enabled = client.put("/v1/plugins/authorization", json={"commands_enabled": True})
    assert enabled.status_code == 200
    assert enabled.json() == {"data": {"commands_enabled": True}}
    assert client.get("/v1/plugins/authorization").json() == {"data": {"commands_enabled": True}}

    disabled = client.put("/v1/plugins/authorization", json={"commands_enabled": False})
    assert disabled.json() == {"data": {"commands_enabled": False}}
    assert client.get("/v1/plugins/authorization").json() == {"data": {"commands_enabled": False}}


@pytest.mark.parametrize("body", [{}, {"commands_enabled": "not-a-bool"}])
def test_authorization_rejects_bad_body(client: TestClient, body: dict[str, object]) -> None:
    """缺字段 / 值不是布尔：422，绝不悄悄按 ``false`` 落库。"""
    response = client.put("/v1/plugins/authorization", json=body)
    assert response.status_code == 422
    assert client.get("/v1/plugins/authorization").json() == {"data": {"commands_enabled": False}}


def test_authorization_state_is_per_app_instance(client: TestClient, tmp_path: Path) -> None:
    """状态挂在 ``app.state`` 上：一个实例打开开关，不会串到另一个实例。"""
    client.put("/v1/plugins/authorization", json={"commands_enabled": True})
    with TestClient(create_app(make_settings(tmp_path / "second"))) as other:
        assert other.get("/v1/plugins/authorization").json() == {"data": {"commands_enabled": False}}


# ------------------------------------------------------------------ 鉴权
def test_auth_disabled_needs_no_token(client: TestClient) -> None:
    """默认配置下插件的每个端点都不看 Authorization 头。"""
    assert client.get("/v1/plugins").status_code == 200
    assert client.get("/v1/plugins/status").status_code == 200
    assert client.get("/v1/plugins/authorization").status_code == 200
    response = client.post("/v1/plugins/demo/install", json={}, headers={"Authorization": "Bearer nope"})
    assert response.status_code == 501  # 501 说明请求确实进到了路由，而不是被 401 拦下


def test_plugins_are_not_exempt_paths() -> None:
    """新端点没有给自己开口子：豁免名单仍旧只有那三个握手端点。"""
    assert EXEMPT_PATHS == {"/healthz", "/readyz", "/v1/auth/session"}
    assert not any(path.startswith("/v1/plugins") for path in EXEMPT_PATHS)


def test_plugins_need_token_when_auth_enabled(guarded_client: TestClient) -> None:
    assert guarded_client.get("/v1/plugins").status_code == 401
    assert guarded_client.get("/v1/plugins/status").status_code == 401
    assert guarded_client.get("/v1/plugins/authorization").status_code == 401
    assert guarded_client.put("/v1/plugins/authorization", json={"commands_enabled": True}).status_code == 401
    assert guarded_client.post("/v1/plugins/demo/approve", json={"approved": True}).status_code == 401


def test_wrong_token_is_401(guarded_client: TestClient, device_token: str) -> None:
    """前缀相同的错 token 也不行：比较是常量时间的全串匹配，不是前缀匹配。"""
    response = guarded_client.get("/v1/plugins", headers={"Authorization": f"Bearer {device_token}-wrong"})
    assert response.status_code == 401


def test_plugins_work_with_token(guarded_client: TestClient, device_token: str) -> None:
    headers = {"Authorization": f"Bearer {device_token}"}
    assert guarded_client.get("/v1/plugins", headers=headers).json() == {"data": []}
    status = guarded_client.get("/v1/plugins/status", headers=headers)
    assert status.json()["data"]["enabled"] is False
    granted = guarded_client.put("/v1/plugins/authorization", json={"commands_enabled": True}, headers=headers)
    assert granted.json() == {"data": {"commands_enabled": True}}
    still_missing = guarded_client.post("/v1/plugins/demo/test", headers=headers)
    assert still_missing.status_code == 501
