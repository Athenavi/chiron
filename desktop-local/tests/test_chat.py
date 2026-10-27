"""本地对话闭环（契约 §4）：``/v1/chat/*`` 与 ``/v1/models*``。

测试**不碰真实 PostgreSQL、不碰真实推理代理**：``app.state.local_store`` 与
``app.state.local_inference`` 是运行时留出的注入点，这里换成内存实现。
真实 PG 的建表/读写由 ``scripts/verify_p0.py`` 与人工联调覆盖，本文件不假装测过。
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from app.api import chat as chat_api
from app.config import Settings
from app.main import create_app
from app.runtime.inference import (
    InferenceUpstreamError,
    ModelConfigError,
    load_configured_models,
)
from fastapi.testclient import TestClient


def _now() -> str:
    return datetime.now(UTC).isoformat()


class FakeStore:
    """内存版 store：形状与 ``runtime.store.LocalStore`` 一致（时间去 ISO、messages 正序）。"""

    tenant_id = 1
    user_id = "local"

    def __init__(self) -> None:
        self.sessions: dict[str, dict[str, Any]] = {}
        self.messages: list[dict[str, Any]] = []
        self.agents: dict[str, dict[str, Any]] = {}
        self.schema_calls = 0

    async def ensure_schema(self) -> None:
        self.schema_calls += 1

    # --- 会话 ---
    async def create_session(
        self,
        *,
        session_id: str | None = None,
        title: str = "",
        model: str = "",
        agent_id: str | None = None,
    ) -> dict[str, Any]:
        new_session_id = session_id or f"sess-{len(self.sessions) + 1}"
        now = _now()
        session = {
            "id": new_session_id,
            "tenant_id": self.tenant_id,
            "user_id": self.user_id,
            "title": title,
            "model": model,
            "agent_id": agent_id,
            "message_count": 0,
            "created_at": now,
            "updated_at": now,
        }
        self.sessions[new_session_id] = session
        return dict(session)

    async def get_session(self, session_id: str) -> dict[str, Any] | None:
        session = self.sessions.get(session_id)
        return dict(session) if session else None

    async def list_sessions(self, *, limit: int = 50, offset: int = 0) -> list[dict[str, Any]]:
        ordered = sorted(self.sessions.values(), key=lambda item: item["updated_at"], reverse=True)
        return [dict(item) for item in ordered[offset : offset + limit]]

    async def touch_session(
        self,
        session_id: str,
        *,
        model: str | None = None,
        agent_id: str | None = None,
        title: str | None = None,
    ) -> None:
        session = self.sessions.get(session_id)
        if session is None:
            return
        if model is not None:
            session["model"] = model
        if agent_id is not None:
            session["agent_id"] = agent_id
        if title is not None:
            session["title"] = title
        session["updated_at"] = _now()

    # --- 消息 ---
    async def append_message(
        self,
        *,
        session_id: str,
        role: str,
        content: str,
        model: str = "",
        agent_id: str | None = None,
        prompt_tokens: int | None = None,
        completion_tokens: int | None = None,
        message_id: str | None = None,
    ) -> dict[str, Any]:
        created_at = _now()
        message = {
            "id": message_id or f"msg-{len(self.messages) + 1}",
            "seq": len(self.messages) + 1,
            "session_id": session_id,
            "tenant_id": self.tenant_id,
            "user_id": self.user_id,
            "role": role,
            "content": content,
            "model": model,
            "agent_id": agent_id,
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
            "created_at": created_at,
        }
        self.messages.append(message)
        session = self.sessions[session_id]
        session["message_count"] += 1
        session["updated_at"] = created_at
        return dict(message)

    async def list_messages(self, *, session_id: str, limit: int = 50) -> list[dict[str, Any]]:
        rows = [item for item in self.messages if item["session_id"] == session_id]
        return [dict(item) for item in rows[-limit:]]

    # --- Agent ---
    async def create_agent(self, **fields: Any) -> dict[str, Any]:
        agent_id = fields.pop("agent_id", None) or f"agt-{len(self.agents) + 1}"
        now = _now()
        agent = {
            "id": agent_id,
            "tenant_id": self.tenant_id,
            "user_id": self.user_id,
            "name": fields.get("name", ""),
            "description": fields.get("description", ""),
            "system_prompt": fields.get("system_prompt", ""),
            "model": fields.get("model", ""),
            "tools": list(fields.get("tools") or []),
            "enabled": bool(fields.get("enabled", True)),
            "created_at": now,
            "updated_at": now,
        }
        self.agents[agent_id] = agent
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
        for key, value in fields.items():
            if key in {"name", "description", "system_prompt", "model", "tools", "enabled"}:
                agent[key] = list(value or []) if key == "tools" else value
        agent["updated_at"] = _now()
        return dict(agent)

    async def delete_agent(self, agent_id: str) -> bool:
        return self.agents.pop(agent_id, None) is not None


class FakeInference:
    """假推理客户端：记录每次调用，按需返回内容或抛错。"""

    def __init__(
        self,
        reply: str = "这是本地模型的回复。",
        usage: dict[str, int] | None = None,
        error: Exception | None = None,
        provider: str = "fake",
    ) -> None:
        self.reply = reply
        self.usage = usage if usage is not None else {"prompt_tokens": 11, "completion_tokens": 7, "total_tokens": 18}
        self.error = error
        self.provider = provider
        self.calls: list[dict[str, Any]] = []

    async def infer(self, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(kwargs)
        if self.error is not None:
            raise self.error
        return {
            "text": self.reply,
            "model": kwargs["model"],
            "provider": self.provider,
            "usage": dict(self.usage),
            "usage_estimated": False,
            "latency_ms": 1.0,
        }


def _make_app(tmp_path: Path, *, store: Any = None, inference: Any = None) -> Any:
    """建一个只接线了 chat 路由的 app（``/v1/models`` 由 ``create_app`` 自带）。"""
    settings = Settings(data_dir=tmp_path / "data", pg_enabled=False, redis_enabled=False)
    app = create_app(settings)
    app.include_router(chat_api.router)
    app.state.local_store = store
    app.state.local_inference = inference
    return app


@pytest.fixture
def parts(tmp_path: Path) -> Iterator[tuple[TestClient, FakeStore, FakeInference]]:
    store = FakeStore()
    inference = FakeInference()
    with TestClient(_make_app(tmp_path, store=store, inference=inference)) as client:
        yield client, store, inference


@pytest.fixture
def plain_client(tmp_path: Path) -> Iterator[TestClient]:
    """什么都没注入的客户端：用来验证 503 分支。"""
    with TestClient(_make_app(tmp_path)) as client:
        yield client


def parse_sse(body: str) -> list[dict[str, Any]]:
    """把 SSE 正文解析成事件列表（每个 ``data:`` 一行一个 JSON）。"""
    events: list[dict[str, Any]] = []
    for block in body.strip().split("\n\n"):
        for line in block.splitlines():
            if line.startswith("data: "):
                events.append(json.loads(line[len("data: ") :]))
    return events


# --------------------------------------------------------------------- 提交
def test_submit_returns_message_and_usage(parts: tuple[TestClient, FakeStore, FakeInference]) -> None:
    client, store, inference = parts
    response = client.post("/v1/chat/submit", json={"message": "你好", "model": "qwen2.5:7b"})

    assert response.status_code == 200
    body = response.json()
    assert set(body) == {"data"}
    data = body["data"]
    assert data["session_id"].startswith("sess-")
    assert data["message"]["role"] == "assistant"
    assert data["message"]["content"] == inference.reply
    assert data["usage"] == {"prompt_tokens": 11, "completion_tokens": 7, "total_tokens": 18}
    assert data["usage_estimated"] is False
    assert data["model"] == "qwen2.5:7b"
    assert data["agent_id"] is None

    # 两条消息都落库，且用量写进了 assistant 那条
    assert [message["role"] for message in store.messages] == ["user", "assistant"]
    assert store.messages[1]["prompt_tokens"] == 11
    assert store.sessions[data["session_id"]]["message_count"] == 2
    # 会话标题取自首条用户消息
    assert store.sessions[data["session_id"]]["title"] == "你好"


def test_submit_passes_sampling_params(parts: tuple[TestClient, FakeStore, FakeInference]) -> None:
    client, _store, inference = parts
    client.post(
        "/v1/chat/submit",
        json={"message": "hi", "model": "m1", "max_tokens": 64, "temperature": 0.2},
    )

    call = inference.calls[0]
    assert call["model"] == "m1"
    assert call["max_tokens"] == 64
    assert call["temperature"] == 0.2
    # tenant/user 来自本地单租户约定，照发给代理（便于将来做配额）
    assert call["tenant_id"] == 1
    assert call["user_id"] == "local"


def test_submit_reuses_session_and_sends_history(parts: tuple[TestClient, FakeStore, FakeInference]) -> None:
    client, _store, inference = parts
    first = client.post("/v1/chat/submit", json={"message": "第一条", "model": "m1"}).json()["data"]
    second = client.post(
        "/v1/chat/submit",
        json={"message": "第二条", "model": "m1", "session_id": first["session_id"]},
    ).json()["data"]

    assert second["session_id"] == first["session_id"]
    messages = inference.calls[-1]["messages"]
    assert [item["role"] for item in messages] == ["user", "assistant", "user"]
    assert messages[0]["content"] == "第一条"
    assert messages[1]["content"] == inference.reply
    assert messages[2]["content"] == "第二条"


def test_submit_with_agent_prepends_system_prompt(parts: tuple[TestClient, FakeStore, FakeInference]) -> None:
    client, store, inference = parts
    agent = store.agents["agt-x"] = {
        "id": "agt-x",
        "tenant_id": 1,
        "user_id": "local",
        "name": "翻译官",
        "description": "",
        "system_prompt": "只输出译文",
        "model": "m-agent",
        "tools": [],
        "enabled": True,
        "created_at": _now(),
        "updated_at": _now(),
    }

    data = client.post("/v1/chat/submit", json={"message": "hello", "agent_id": agent["id"]}).json()["data"]

    assert inference.calls[-1]["messages"][0] == {"role": "system", "content": "只输出译文"}
    # 没给 model 时用 Agent 的默认模型
    assert inference.calls[-1]["model"] == "m-agent"
    assert data["model"] == "m-agent"
    assert data["agent_id"] == "agt-x"
    # 会话记住这次用的 Agent
    assert store.sessions[data["session_id"]]["agent_id"] == "agt-x"


def test_submit_unknown_session_is_404(parts: tuple[TestClient, FakeStore, FakeInference]) -> None:
    client, _store, inference = parts
    response = client.post("/v1/chat/submit", json={"message": "hi", "model": "m1", "session_id": "nope"})

    assert response.status_code == 404
    assert "nope" in response.json()["detail"]
    assert inference.calls == []


def test_submit_unknown_agent_is_404(parts: tuple[TestClient, FakeStore, FakeInference]) -> None:
    client, _store, _inference = parts
    response = client.post("/v1/chat/submit", json={"message": "hi", "model": "m1", "agent_id": "ghost"})

    assert response.status_code == 404
    assert "ghost" in response.json()["detail"]


def test_submit_empty_message_is_422(parts: tuple[TestClient, FakeStore, FakeInference]) -> None:
    client, _store, _inference = parts
    assert client.post("/v1/chat/submit", json={"message": ""}).status_code == 422
    assert client.post("/v1/chat/submit", json={}).status_code == 422


def test_submit_without_model_is_400(parts: tuple[TestClient, FakeStore, FakeInference]) -> None:
    """没有任何模型来源时明确报 400，不要编造一个默认模型名去调代理。"""
    client, store, _inference = parts
    response = client.post("/v1/chat/submit", json={"message": "hi"})

    assert response.status_code == 400
    assert "model" in response.json()["detail"]
    assert store.messages == []


def test_submit_uses_configured_model(
    parts: tuple[TestClient, FakeStore, FakeInference],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client, _store, inference = parts
    monkeypatch.setenv("CHIRON_LOCAL_MODELS", json.dumps(["qwen2.5:7b", "llama3.2"]))

    response = client.post("/v1/chat/submit", json={"message": "hi"})

    assert response.status_code == 200
    assert inference.calls[-1]["model"] == "qwen2.5:7b"


def test_submit_inference_failure_is_502_and_keeps_user_message(
    parts: tuple[TestClient, FakeStore, FakeInference],
) -> None:
    client, store, inference = parts
    inference.error = InferenceUpstreamError("推理代理返回 HTTP 500")

    response = client.post("/v1/chat/submit", json={"message": "别丢了我", "model": "m1"})

    assert response.status_code == 502
    assert "HTTP 500" in response.json()["detail"]
    # 用户输入仍然落库（模块 docstring 第 1 条取舍）
    assert [message["role"] for message in store.messages] == ["user"]
    assert store.messages[0]["content"] == "别丢了我"


def test_submit_without_storage_is_503(plain_client: TestClient) -> None:
    response = plain_client.post("/v1/chat/submit", json={"message": "hi", "model": "m1"})

    assert response.status_code == 503
    assert "PostgreSQL" in response.json()["detail"]


def test_submit_without_inference_proxy_is_503(tmp_path: Path) -> None:
    store = FakeStore()
    with TestClient(_make_app(tmp_path, store=store, inference=None)) as client:
        response = client.post("/v1/chat/submit", json={"message": "hi", "model": "m1"})

    assert response.status_code == 503
    assert "推理代理" in response.json()["detail"]


# --------------------------------------------------------------------- SSE
def test_submit_streams_sse_events(parts: tuple[TestClient, FakeStore, FakeInference]) -> None:
    client, _store, inference = parts
    inference.reply = "一二三四五六七八九十" * 4

    response = client.post("/v1/chat/submit", params={"stream": "true"}, json={"message": "hi", "model": "m1"})

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/event-stream")
    assert "event: text" in response.text
    assert "event: done" in response.text

    events = parse_sse(response.text)
    assert events[0]["type"] == "start"
    texts = [event for event in events if event["type"] == "text"]
    assert len(texts) > 1
    assert "".join(event["text"] for event in texts) == inference.reply
    done = events[-1]
    assert done["type"] == "done"
    assert done["message"]["content"] == inference.reply
    assert done["usage"]["total_tokens"] == 18


def test_submit_stream_flag_in_body(parts: tuple[TestClient, FakeStore, FakeInference]) -> None:
    client, _store, _inference = parts
    response = client.post("/v1/chat/submit", json={"message": "hi", "model": "m1", "stream": True})

    assert response.headers["content-type"].startswith("text/event-stream")
    assert parse_sse(response.text)[-1]["type"] == "done"


# --------------------------------------------------------------------- 会话与消息
def test_list_sessions_and_messages(parts: tuple[TestClient, FakeStore, FakeInference]) -> None:
    client, _store, _inference = parts
    first = client.post("/v1/chat/submit", json={"message": "会话一", "model": "m1"}).json()["data"]
    client.post("/v1/chat/submit", json={"message": "会话二", "model": "m1"})

    sessions = client.get("/v1/chat/sessions").json()["data"]
    assert len(sessions) == 2
    assert first["session_id"] in {session["id"] for session in sessions}
    assert all(session["message_count"] == 2 for session in sessions)
    # 最近更新的排前面
    assert sessions[0]["title"] == "会话二"

    messages = client.get(f"/v1/chat/sessions/{first['session_id']}/messages").json()
    assert [item["role"] for item in messages["data"]] == ["user", "assistant"]
    assert messages["session"]["id"] == first["session_id"]


def test_messages_of_unknown_session_is_404(parts: tuple[TestClient, FakeStore, FakeInference]) -> None:
    client, _store, _inference = parts
    response = client.get("/v1/chat/sessions/nope/messages")

    assert response.status_code == 404
    assert "nope" in response.json()["detail"]


def test_sessions_limit_is_clamped(parts: tuple[TestClient, FakeStore, FakeInference]) -> None:
    client, store, _inference = parts
    for _ in range(3):
        client.post("/v1/chat/submit", json={"message": "hi", "model": "m1"})

    assert len(client.get("/v1/chat/sessions", params={"limit": 1}).json()["data"]) == 1
    # 超过上限不是报错，而是夹到 MAX_SESSION_LIMIT
    assert client.get("/v1/chat/sessions", params={"limit": 10_000}).status_code == 200
    assert len(store.sessions) == 3


# --------------------------------------------------------------------- 模型
def test_models_default_empty(parts: tuple[TestClient, FakeStore, FakeInference]) -> None:
    client, _store, _inference = parts
    response = client.get("/v1/models")

    assert response.status_code == 200
    assert response.json() == {"data": []}


def test_models_read_from_env(
    parts: tuple[TestClient, FakeStore, FakeInference],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client, _store, _inference = parts
    monkeypatch.setenv(
        "CHIRON_LOCAL_MODELS",
        json.dumps(["llama3.2", {"id": "qwen2.5:7b", "label": "Qwen2.5 7B", "context_window": 32768}]),
    )

    data = client.get("/v1/models").json()["data"]

    assert data == [
        {"id": "llama3.2"},
        {"id": "qwen2.5:7b", "label": "Qwen2.5 7B", "context_window": 32768},
    ]


def test_models_invalid_json_is_500(
    parts: tuple[TestClient, FakeStore, FakeInference],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client, _store, _inference = parts
    monkeypatch.setenv("CHIRON_LOCAL_MODELS", "[not json")

    response = client.get("/v1/models")

    assert response.status_code == 500
    assert response.json()["data"] == []
    assert "CHIRON_LOCAL_MODELS" in response.json()["error"]


def test_models_verify_ok(parts: tuple[TestClient, FakeStore, FakeInference]) -> None:
    client, _store, inference = parts
    inference.reply = "pong"

    response = client.post("/v1/models/verify", json={"model": "qwen2.5:7b"})

    assert response.status_code == 200
    data = response.json()["data"]
    assert data["ok"] is True
    assert data["model"] == "qwen2.5:7b"
    assert data["reply"] == "pong"
    assert inference.calls[-1]["max_tokens"] == 16
    assert inference.calls[-1]["messages"] == [{"role": "user", "content": "ping"}]


def test_models_verify_uses_first_configured_model(
    parts: tuple[TestClient, FakeStore, FakeInference],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client, _store, inference = parts
    monkeypatch.setenv("CHIRON_LOCAL_MODELS", json.dumps(["first-model", "second-model"]))

    data = client.post("/v1/models/verify", json={}).json()["data"]

    assert data["ok"] is True
    assert inference.calls[-1]["model"] == "first-model"


def test_models_verify_without_model_is_400(parts: tuple[TestClient, FakeStore, FakeInference]) -> None:
    client, _store, _inference = parts
    response = client.post("/v1/models/verify", json={})

    assert response.status_code == 400
    assert "model" in response.json()["detail"]


def test_models_verify_reports_upstream_failure(parts: tuple[TestClient, FakeStore, FakeInference]) -> None:
    client, _store, inference = parts
    inference.error = InferenceUpstreamError("推理代理返回 HTTP 503")

    response = client.post("/v1/models/verify", json={"model": "m1"})

    assert response.status_code == 502
    assert response.json()["data"]["ok"] is False
    assert "HTTP 503" in response.json()["data"]["error"]


def test_models_verify_without_proxy_is_503(tmp_path: Path) -> None:
    with TestClient(_make_app(tmp_path, store=FakeStore(), inference=None)) as client:
        response = client.post("/v1/models/verify", json={"model": "m1"})

    assert response.status_code == 503
    assert "推理代理" in response.json()["detail"]


# --------------------------------------------------------------------- 纯函数
def test_load_configured_models_shapes() -> None:
    assert load_configured_models("") == []
    assert load_configured_models("  ") == []
    assert load_configured_models(json.dumps(["a", "b", "a"])) == [{"id": "a"}, {"id": "b"}]
    assert load_configured_models(json.dumps({"models": ["a"]})) == [{"id": "a"}]
    assert load_configured_models(json.dumps([{"name": "c"}])) == [{"id": "c"}]


def test_load_configured_models_rejects_bad_config() -> None:
    with pytest.raises(ModelConfigError):
        load_configured_models("[]]")
    with pytest.raises(ModelConfigError):
        load_configured_models(json.dumps({"models": "nope"}))
    with pytest.raises(ModelConfigError):
        load_configured_models(json.dumps([{"label": "no id"}]))
    with pytest.raises(ModelConfigError):
        load_configured_models(json.dumps([42]))
