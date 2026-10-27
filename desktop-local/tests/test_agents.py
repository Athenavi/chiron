"""``/v1/agents`` 的 CRUD（契约 §4）。

与 ``test_chat.py`` 一样，PG 用内存实现替换：这里只验证**路由行为**（校验、状态码、
``{"data": ...}`` 包装、字段局部更新），真实 SQL 由人工联调与 ``verify_p0.py`` 覆盖。
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from app.api import agents as agents_api
from app.config import Settings
from app.main import create_app
from fastapi.testclient import TestClient

UPDATABLE = ("name", "description", "system_prompt", "model", "tools", "enabled")


def _now() -> str:
    return datetime.now(UTC).isoformat()


class FakeAgentStore:
    """只实现 Agent 相关的内存 store（形状与 ``LocalStore`` 一致）。"""

    tenant_id = 1
    user_id = "local"

    def __init__(self) -> None:
        self.agents: dict[str, dict[str, Any]] = {}
        self.schema_calls = 0

    async def ensure_schema(self) -> None:
        self.schema_calls += 1

    async def create_agent(
        self,
        *,
        name: str,
        description: str = "",
        system_prompt: str = "",
        model: str = "",
        tools: list[str] | None = None,
        enabled: bool = True,
        agent_id: str | None = None,
    ) -> dict[str, Any]:
        new_agent_id = agent_id or f"agt-{len(self.agents) + 1}"
        now = _now()
        agent = {
            "id": new_agent_id,
            "tenant_id": self.tenant_id,
            "user_id": self.user_id,
            "name": name,
            "description": description,
            "system_prompt": system_prompt,
            "model": model,
            "tools": list(tools or []),
            "enabled": enabled,
            "created_at": now,
            "updated_at": now,
        }
        self.agents[new_agent_id] = agent
        return dict(agent)

    async def get_agent(self, agent_id: str) -> dict[str, Any] | None:
        agent = self.agents.get(agent_id)
        return dict(agent) if agent else None

    async def list_agents(self, *, limit: int = 100, offset: int = 0) -> list[dict[str, Any]]:
        return [dict(item) for item in list(self.agents.values())[offset : offset + limit]]

    async def update_agent(self, agent_id: str, **fields: Any) -> dict[str, Any] | None:
        agent = self.agents.get(agent_id)
        if agent is None:
            return None
        changed = False
        for key, value in fields.items():
            if key in UPDATABLE:
                agent[key] = list(value or []) if key == "tools" else value
                changed = True
        if changed:
            agent["updated_at"] = _now()
        return dict(agent)

    async def delete_agent(self, agent_id: str) -> bool:
        return self.agents.pop(agent_id, None) is not None


def _make_app(tmp_path: Path, *, store: Any = None) -> Any:
    settings = Settings(data_dir=tmp_path / "data", pg_enabled=False, redis_enabled=False)
    app = create_app(settings)
    app.include_router(agents_api.router)
    app.state.local_store = store
    return app


@pytest.fixture
def parts(tmp_path: Path) -> Iterator[tuple[TestClient, FakeAgentStore]]:
    store = FakeAgentStore()
    with TestClient(_make_app(tmp_path, store=store)) as client:
        yield client, store


# --------------------------------------------------------------------- 列表 / 创建
def test_list_agents_is_empty_initially(parts: tuple[TestClient, FakeAgentStore]) -> None:
    client, _store = parts
    response = client.get("/v1/agents")

    assert response.status_code == 200
    assert response.json() == {"data": []}


def test_create_agent_returns_full_record(parts: tuple[TestClient, FakeAgentStore]) -> None:
    client, store = parts
    response = client.post(
        "/v1/agents",
        json={
            "name": "翻译官",
            "description": "把中文译成英文",
            "system_prompt": "只输出译文",
            "model": "qwen2.5:7b",
            "tools": ["web_search"],
        },
    )

    assert response.status_code == 201
    data = response.json()["data"]
    assert data["id"].startswith("agt-")
    assert data["name"] == "翻译官"
    assert data["description"] == "把中文译成英文"
    assert data["system_prompt"] == "只输出译文"
    assert data["model"] == "qwen2.5:7b"
    assert data["tools"] == ["web_search"]
    assert data["enabled"] is True
    assert data["tenant_id"] == 1
    assert data["user_id"] == "local"
    assert isinstance(data["created_at"], str)
    assert store.agents[data["id"]]["name"] == "翻译官"
    # 建表检查在对每个请求都发生（幂等，由 store 自己短路）
    assert store.schema_calls >= 1


def test_create_agent_defaults(parts: tuple[TestClient, FakeAgentStore]) -> None:
    client, _store = parts
    data = client.post("/v1/agents", json={"name": "极简"}).json()["data"]

    assert data["description"] == ""
    assert data["system_prompt"] == ""
    assert data["model"] == ""
    assert data["tools"] == []
    assert data["enabled"] is True


def test_create_agent_requires_name(parts: tuple[TestClient, FakeAgentStore]) -> None:
    client, _store = parts
    assert client.post("/v1/agents", json={}).status_code == 422
    assert client.post("/v1/agents", json={"name": ""}).status_code == 422
    assert client.post("/v1/agents", json={"name": "x" * 121}).status_code == 422


def test_list_agents_after_create(parts: tuple[TestClient, FakeAgentStore]) -> None:
    client, _store = parts
    client.post("/v1/agents", json={"name": "A"})
    client.post("/v1/agents", json={"name": "B"})

    data = client.get("/v1/agents").json()["data"]
    assert [item["name"] for item in data] == ["A", "B"]
    assert len(client.get("/v1/agents", params={"limit": 1}).json()["data"]) == 1


# --------------------------------------------------------------------- 单个
def test_get_agent(parts: tuple[TestClient, FakeAgentStore]) -> None:
    client, _store = parts
    created = client.post("/v1/agents", json={"name": "A"}).json()["data"]

    response = client.get(f"/v1/agents/{created['id']}")

    assert response.status_code == 200
    assert response.json()["data"]["id"] == created["id"]


def test_get_unknown_agent_is_404(parts: tuple[TestClient, FakeAgentStore]) -> None:
    client, _store = parts
    response = client.get("/v1/agents/ghost")

    assert response.status_code == 404
    assert "ghost" in response.json()["detail"]


# --------------------------------------------------------------------- 更新
def test_patch_agent_touches_only_given_fields(parts: tuple[TestClient, FakeAgentStore]) -> None:
    client, _store = parts
    created = client.post(
        "/v1/agents",
        json={"name": "A", "description": "旧描述", "system_prompt": "旧提示", "model": "m1", "tools": ["t1"]},
    ).json()["data"]

    data = client.patch(f"/v1/agents/{created['id']}", json={"description": "新描述", "enabled": False}).json()["data"]

    assert data["description"] == "新描述"
    assert data["enabled"] is False
    # 没传的字段保持原值
    assert data["name"] == "A"
    assert data["system_prompt"] == "旧提示"
    assert data["model"] == "m1"
    assert data["tools"] == ["t1"]


def test_patch_agent_can_replace_tools_and_name(parts: tuple[TestClient, FakeAgentStore]) -> None:
    client, _store = parts
    created = client.post("/v1/agents", json={"name": "A", "tools": ["t1"]}).json()["data"]

    data = client.patch(f"/v1/agents/{created['id']}", json={"name": "B", "tools": []}).json()["data"]

    assert data["name"] == "B"
    assert data["tools"] == []


def test_patch_unknown_agent_is_404(parts: tuple[TestClient, FakeAgentStore]) -> None:
    client, _store = parts
    assert client.patch("/v1/agents/ghost", json={"name": "X"}).status_code == 404


def test_patch_with_empty_body_keeps_record(parts: tuple[TestClient, FakeAgentStore]) -> None:
    client, store = parts
    created = client.post("/v1/agents", json={"name": "A"}).json()["data"]

    response = client.patch(f"/v1/agents/{created['id']}", json={})

    assert response.status_code == 200
    assert response.json()["data"]["name"] == "A"
    assert store.agents[created["id"]]["updated_at"] == created["updated_at"]


def test_patch_rejects_empty_name(parts: tuple[TestClient, FakeAgentStore]) -> None:
    client, _store = parts
    created = client.post("/v1/agents", json={"name": "A"}).json()["data"]

    assert client.patch(f"/v1/agents/{created['id']}", json={"name": ""}).status_code == 422


# --------------------------------------------------------------------- 删除
def test_delete_agent_then_404(parts: tuple[TestClient, FakeAgentStore]) -> None:
    client, store = parts
    created = client.post("/v1/agents", json={"name": "A"}).json()["data"]

    response = client.delete(f"/v1/agents/{created['id']}")

    assert response.status_code == 200
    assert response.json()["data"] == {"id": created["id"], "deleted": True}
    assert store.agents == {}
    assert client.get(f"/v1/agents/{created['id']}").status_code == 404
    assert client.delete(f"/v1/agents/{created['id']}").status_code == 404


# --------------------------------------------------------------------- 存储不可用
def test_agents_without_storage_is_503(tmp_path: Path) -> None:
    with TestClient(_make_app(tmp_path)) as client:
        assert client.get("/v1/agents").status_code == 503
        assert client.post("/v1/agents", json={"name": "A"}).status_code == 503
        assert client.patch("/v1/agents/agt-1", json={"name": "B"}).status_code == 503
        assert client.delete("/v1/agents/agt-1").status_code == 503
        detail = client.get("/v1/agents").json()["detail"]
    assert "PostgreSQL" in detail
