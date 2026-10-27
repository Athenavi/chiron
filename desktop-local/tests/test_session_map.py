"""会话地图两族端点的回归测试。

真机故障：本地模式下地图白屏，``adapter.js`` 的两族数据面无一路由
（``/v1/session-map/*`` 与 ``/v1/conversations`` 只存在于 Go 网关），实测双双 404。
这里钉住三件事：

1. 端点**存在**（不再 404），并且形状与 ``adapter.js`` 的读取方式一致。
2. 存储不可用时如实 503，而不是假装成功（地图会显示"读取失败"而不是空白画布）。
3. 请求体非法时 400 / 超限拒绝，避免异常客户端把库撑爆。

不碰真实 PostgreSQL：会话走注入的假 store，画布走"无 DSN → 503"分支。
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from app.api import conversations as conversations_api
from app.api import session_map as session_map_api
from app.config import Settings
from app.main import create_app
from fastapi.testclient import TestClient

_MESSAGE = {
    "id": "msg_1",
    "seq": 1,
    "session_id": "sess_1",
    "role": "user",
    "content": "你好",
    "model": "mock",
    "agent_id": None,
    "prompt_tokens": 1,
    "completion_tokens": 0,
    "created_at": "2026-10-27T00:00:00+00:00",
}

_SESSION = {
    "id": "sess_1",
    "title": "第一次对话",
    "model": "mock",
    "agent_id": None,
    "message_count": 1,
    "created_at": "2026-10-27T00:00:00+00:00",
    "updated_at": "2026-10-27T00:00:00+00:00",
}


class FakeStore:
    """只实现这两族端点用到的方法。"""

    def __init__(self) -> None:
        self.sessions = [dict(_SESSION)]
        self.messages = [dict(_MESSAGE)]
        self.ensure_calls = 0

    async def ensure_schema(self) -> None:
        self.ensure_calls += 1

    async def list_sessions(self, limit: int = 20, offset: int = 0) -> list[dict[str, Any]]:
        return self.sessions[offset : offset + limit]

    async def get_session(self, session_id: str) -> dict[str, Any] | None:
        return next((s for s in self.sessions if s["id"] == session_id), None)

    async def list_messages(self, session_id: str, limit: int = 200) -> list[dict[str, Any]]:
        return [m for m in self.messages if m["session_id"] == session_id][:limit]


def _make_app(tmp_path: Path, *, store: Any = None) -> Any:
    # pg_enabled=False：画布路由拿不到 DSN，正好覆盖 503 分支
    settings = Settings(data_dir=tmp_path / "data", pg_enabled=False, redis_enabled=False)
    app = create_app(settings)
    app.state.local_store = store
    return app


@pytest.fixture
def client(tmp_path: Path) -> Iterator[TestClient]:
    with TestClient(_make_app(tmp_path, store=FakeStore())) as c:
        yield c


@pytest.fixture
def bare_client(tmp_path: Path) -> Iterator[TestClient]:
    """没注入 store / 没有 DSN：验证 503 而非 500。"""
    with TestClient(_make_app(tmp_path)) as c:
        yield c


# ── /v1/conversations ────────────────────────────────────────────────


def test_conversation_list_matches_adapter_shape(client: TestClient) -> None:
    """``adapter.js`` 的 ``importRecentSessions`` 读 ``unwrap(body)`` 后取数组。"""
    r = client.get("/v1/conversations", params={"per_page": 30})
    assert r.status_code == 200, r.text
    data = r.json()["data"]
    assert isinstance(data, list) and len(data) == 1
    item = data[0]
    assert item["id"] == "sess_1"
    # 云端才有的字段也要存在（空值）：地图的渲染分支不做 undefined 判断
    for key in ("title", "tag", "alias", "parent_title", "branch_from_seq"):
        assert key in item


def test_conversation_detail_carries_messages(client: TestClient) -> None:
    """``loadMessages`` 读 ``data.messages``，消息字段必须是 ``role``/``content``。"""
    r = client.get("/v1/conversations/sess_1", params={"limit": 200})
    assert r.status_code == 200, r.text
    data = r.json()["data"]
    assert data["id"] == "sess_1"
    assert data["messages"][0]["role"] == "user"
    assert data["messages"][0]["content"] == "你好"


def test_conversation_detail_404_for_unknown(client: TestClient) -> None:
    assert client.get("/v1/conversations/nope").status_code == 404


def test_conversation_list_503_without_store(bare_client: TestClient) -> None:
    """没有 store 时不能 500 —— 地图要能显示"本地库不可用"。"""
    assert bare_client.get("/v1/conversations").status_code == 503


def test_adapter_only_endpoints_answer_501_not_404(client: TestClient) -> None:
    """删除/分叉由宿主 RPC 代理；明确 501 比静默 404 好排查。"""
    assert client.delete("/v1/conversations/sess_1").status_code == 501
    assert client.post("/v1/conversations/sess_1/fork").status_code == 501


# ── /v1/session-map/workspaces ───────────────────────────────────────


def test_workspace_list_503_without_dsn(bare_client: TestClient) -> None:
    """画布直连 PG；本地库没起来时必须是 503（旧行为是 404 + 白屏）。"""
    r = bare_client.get("/v1/session-map/workspaces")
    assert r.status_code == 503, r.text
    assert "PostgreSQL" in r.json()["detail"]


def test_workspace_routes_are_registered(bare_client: TestClient) -> None:
    """路由存在性：修复前这些路径一律 404。"""
    paths = bare_client.app.openapi()["paths"]
    for path in (
        "/v1/session-map/workspaces",
        "/v1/session-map/workspaces/{workspace_id}",
        "/v1/session-map/workspaces/{workspace_id}/fork-node",
        "/v1/conversations",
        "/v1/conversations/{conversation_id}",
    ):
        assert path in paths, f"路由未注册：{path}"


def test_workspace_put_rejects_non_array_nodes(bare_client: TestClient) -> None:
    """请求体校验先于连接：非法 nodes 应该 400，不该等到 PG 超时。"""
    r = bare_client.put("/v1/session-map/workspaces/w1", json={"nodes": "oops"})
    assert r.status_code == 400


def test_workspace_put_rejects_too_many_nodes(bare_client: TestClient) -> None:
    payload = {"nodes": [{"id": f"n{i}"} for i in range(session_map_api.MAX_NODES + 1)]}
    r = bare_client.put("/v1/session-map/workspaces/w1", json=payload)
    assert r.status_code == 400
    assert str(session_map_api.MAX_NODES) in r.json()["detail"]


# ── CORS：iframe 跨源的前提 ──────────────────────────────────────────


def test_cors_allows_file_origin(bare_client: TestClient) -> None:
    """打包版 iframe 的 Origin 是字面量 ``"null"``；没有这个头，浏览器拦掉全部响应。

    这里用 ``file://`` 触发 ``allow_origin_regex``（Starlette 对 ``"null"`` 的
    匹配取决于是否把该值当作 origin，两种都覆盖）。
    """
    for origin in ("file://", "null"):
        r = bare_client.get("/healthz", headers={"Origin": origin})
        assert r.status_code == 200
        assert r.headers.get("access-control-allow-origin") == origin, origin


def test_cors_rejects_foreign_origin(bare_client: TestClient) -> None:
    """不能让任意网站读本地数据。"""
    r = bare_client.get("/healthz", headers={"Origin": "https://evil.example"})
    assert r.headers.get("access-control-allow-origin") is None


def test_cors_preflight_allows_authorization(bare_client: TestClient) -> None:
    """本地鉴权走 Bearer，预检必须放行 Authorization 头。"""
    r = bare_client.options(
        "/v1/conversations",
        headers={
            "Origin": "file://",
            "Access-Control-Request-Method": "GET",
            "Access-Control-Request-Headers": "authorization",
        },
    )
    assert r.status_code in (200, 204)
    assert "authorization" in r.headers.get("access-control-allow-headers", "").lower()


def test_conversations_module_exposes_limits() -> None:
    assert conversations_api.MAX_PER_PAGE == 100
