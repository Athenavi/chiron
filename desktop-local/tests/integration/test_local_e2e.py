"""本地 core 端到端：健康检查 / 会话 / 对话 / Agent（契约 §4），跑在**真实 PostgreSQL** 上。

每个测试下面都有真东西：PG 由 ``PostgresServer`` 起、schema 由 ``alembic upgrade head`` 建、
对话请求真的发到本机 mock 推理服务（``POST /infer``）。没有 ``asyncpg`` / ``httpx`` 替身，
断言里出现的行数都是用 ``psycopg2`` 旁路查真库得到的。

仓库里另有两批测试不重复：

- ``tests/test_chat.py`` / ``test_agents.py``：内存 store，只验证编排与状态码；
- ``scripts/verify_local_e2e.py``：真起 uvicorn 子进程、按 Electron 的方式解析 listening。
"""

from __future__ import annotations

import json

import pytest
from app.main import create_app
from app.runtime.inference import MODELS_ENV
from app.storage.pg import run_sql
from fastapi.testclient import TestClient

from .conftest import MOCK_CONTENT


# --------------------------------------------------------------------- 健康与迁移
def test_healthz_is_pure_process_check(app_client: TestClient) -> None:
    """``/healthz`` 不碰存储：进程活着就 200（Electron 用它判"后端起来了没"）。"""
    response = app_client.get("/healthz")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_readyz_reports_real_postgres_and_migration(
    app_client: TestClient,
    pg_dsn: str,
) -> None:
    """``/readyz`` 必须如实报告真实 PG 的版本与后端名，而不是写死的 ``true``。"""
    response = app_client.get("/readyz")
    body = response.json()
    assert response.status_code == 200, body
    assert body["status"] == "ok"
    assert body["pg"] is True
    assert body["pg_backend"] == "external", body
    assert body["pg_version"], "/readyz 没给出真实 server_version"
    assert body["auth"] == "disabled"
    assert body["errors"] == []

    # 旁路核查：``/readyz`` 报的版本就是这台库的真实版本
    version = str(run_sql(pg_dsn, "SELECT current_setting('server_version')")[0][0])
    assert version == body["pg_version"]


def test_alembic_head_is_applied(pg_server, pg_dsn: str) -> None:
    """``alembic upgrade head`` 真的落到这个实例里（而不是被 ``.env`` 抢走）。"""
    revision = pg_server.current_revision()
    assert revision, "迁移后读不到 alembic_version"
    rows = run_sql(pg_dsn, "SELECT count(*) FROM alembic_version")
    assert rows[0][0] == 1
    # 仓库 migrations/ 建的业务表也在同一个库里
    tables = {row[0] for row in run_sql(pg_dsn, "SELECT tablename FROM pg_tables WHERE schemaname = 'public'")}
    assert "alembic_version" in tables
    assert len(tables) > 1


# --------------------------------------------------------------------- 会话（鉴权）
def test_auth_session_is_stable_across_restarts(app_client: TestClient, api_settings) -> None:
    """本地会话：无需口令、token 落盘、重启（新 app 实例）后不变。"""
    first = app_client.post("/v1/auth/session")
    assert first.status_code == 200
    data = first.json()["data"]
    assert data["user"] == {"id": "local", "name": "Local", "role": "owner", "tenant_id": 1}
    token = data["token"]
    assert isinstance(token, str)
    assert len(token) >= 20

    # token 真的落在 <data-dir>/auth/device-token（Electron 侧只发一次请求就靠它）
    token_path = api_settings.device_token_path
    assert token_path.is_file()
    assert token_path.read_text(encoding="utf-8").strip() == token

    second = app_client.post("/v1/auth/session")
    assert second.json()["data"]["token"] == token


# --------------------------------------------------------------------- 对话
def test_chat_submit_persists_user_and_assistant(
    app_client: TestClient,
    pg_dsn: str,
    mock_inference,
) -> None:
    """非流式提交：mock 推理 → 落库 → 会话/消息可回读。"""
    mock_inference.reset()
    response = app_client.post(
        "/v1/chat/submit",
        json={"message": "你好，本地记忆", "model": "mock"},
    )
    assert response.status_code == 200, response.text
    data = response.json()["data"]

    assert data["message"]["role"] == "assistant"
    assert data["message"]["content"] == MOCK_CONTENT
    assert data["model"] == "mock"
    assert data["history_count"] == 0  # 首轮：发给代理的消息里除本轮外没有历史
    session_id = data["session_id"]
    assert session_id.startswith("sess_")

    # 推理确实打到了 mock 服务，且带上了本轮用户消息
    sent = mock_inference.calls[-1]["body"]
    assert sent["model"] == "mock"
    assert sent["messages"][-1] == {"role": "user", "content": "你好，本地记忆"}
    assert sent["tenant_id"] == 1
    assert sent["user_id"] == "local"

    # 真实落库（旁路 psycopg2）
    rows = run_sql(
        pg_dsn,
        "SELECT role, content, model FROM local_messages WHERE session_id = %s ORDER BY seq",
        (session_id,),
    )
    assert [row[0] for row in rows] == ["user", "assistant"]
    assert rows[0][1] == "你好，本地记忆"
    assert rows[1][1] == MOCK_CONTENT
    assert rows[1][2] == "mock"

    # 会话列表与消息回读
    sessions = app_client.get("/v1/chat/sessions").json()["data"]
    assert any(item["id"] == session_id for item in sessions)
    messages = app_client.get(f"/v1/chat/sessions/{session_id}/messages").json()["data"]
    assert [item["role"] for item in messages] == ["user", "assistant"]
    assert messages[1]["content"] == MOCK_CONTENT


def test_chat_usage_is_lost_because_of_field_mismatch(
    app_client: TestClient,
    mock_inference,
) -> None:
    """代理按契约 §9 回 ``input_tokens``/``output_tokens``，core 却只读 ``prompt_tokens``/
    ``completion_tokens``（``app/runtime/inference.py::_normalize_usage``），于是把真实计量
    丢掉、退化成按字符估算并置 ``usage_estimated=True``。

    **这是报告里的 app bug #1**，本测试把现状钉住：修好后应改成
    ``usage_estimated is False`` 且 ``total_tokens == 2``。
    """
    mock_inference.reset()
    response = app_client.post("/v1/chat/submit", json={"message": "计数一次", "model": "mock"})
    assert response.status_code == 200, response.text
    data = response.json()["data"]
    assert data["usage_estimated"] is False, "usage 字段名未与契约 §9 对齐（input_tokens/output_tokens）"
    assert data["usage"]["prompt_tokens"] == 1
    assert data["usage"]["completion_tokens"] == 1
    assert data["usage"]["total_tokens"] == 2


def test_chat_stream_emits_start_text_done(app_client: TestClient) -> None:
    """``?stream=true`` 走 SSE：``start`` → ``text``* → ``done``。"""
    response = app_client.post("/v1/chat/submit?stream=true", json={"message": "流式一次", "model": "mock"})
    assert response.status_code == 200, response.text
    assert response.headers["content-type"].startswith("text/event-stream")
    events = [line[len("event: ") :] for line in response.text.splitlines() if line.startswith("event: ")]
    assert events[0] == "start"
    assert events[-1] == "done"
    assert "text" in events
    assert MOCK_CONTENT in response.text


def test_chat_continues_an_existing_session(app_client: TestClient, pg_dsn: str) -> None:
    """带 ``session_id`` 的第二轮：历史里能看到第一轮，消息数累加。"""
    first = app_client.post("/v1/chat/submit", json={"message": "第一轮", "model": "mock"}).json()["data"]
    session_id = first["session_id"]

    second = app_client.post(
        "/v1/chat/submit",
        json={"message": "第二轮", "session_id": session_id},
    )
    assert second.status_code == 200, second.text
    assert second.json()["data"]["session_id"] == session_id
    # 第二轮的历史 = 第一轮 user + assistant（history_count 是本轮发给代理的历史条数）
    assert second.json()["data"]["history_count"] == 2

    rows = run_sql(pg_dsn, "SELECT count(*) FROM local_messages WHERE session_id = %s", (session_id,))
    assert rows[0][0] == 4


def test_chat_error_mapping(app_client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    """错误码：空消息 422/400、会话不存在 404、Agent 不存在 404、没模型 400。"""
    assert app_client.post("/v1/chat/submit", json={"message": ""}).status_code == 422
    assert app_client.post("/v1/chat/submit", json={}).status_code == 422
    # 只有空白字符通过 pydantic，再被 run_turn 拒成 400
    assert app_client.post("/v1/chat/submit", json={"message": "   ", "model": "mock"}).status_code == 400

    missing_session = app_client.post(
        "/v1/chat/submit",
        json={"message": "hi", "session_id": "ses_does_not_exist", "model": "mock"},
    )
    assert missing_session.status_code == 404
    assert "会话不存在" in missing_session.json()["detail"]

    missing_agent = app_client.post(
        "/v1/chat/submit",
        json={"message": "hi", "agent_id": "agt_none", "model": "mock"},
    )
    assert missing_agent.status_code == 404
    assert "Agent 不存在" in missing_agent.json()["detail"]

    # 没给 model、配置清单也为空 → 400（不塞 "default" 让代理去猜）
    monkeypatch.delenv(MODELS_ENV, raising=False)
    no_model = app_client.post("/v1/chat/submit", json={"message": "hi"})
    assert no_model.status_code == 400, no_model.text
    assert "缺少模型" in no_model.json()["detail"]

    assert app_client.get("/v1/chat/sessions/ses_missing/messages").status_code == 404


def test_models_list_and_verify_hit_the_proxy(app_client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    """``/v1/models`` 只认配置；``/v1/models/verify`` 会真的发一次 ``ping``。"""
    monkeypatch.setenv(MODELS_ENV, json.dumps(["mock"]))
    assert app_client.get("/v1/models").json()["data"] == [{"id": "mock"}]

    verified = app_client.post("/v1/models/verify", json={"model": "mock"})
    assert verified.status_code == 200, verified.text
    data = verified.json()["data"]
    assert data["ok"] is True
    assert data["reply"] == MOCK_CONTENT
    assert data["model"] == "mock"


# --------------------------------------------------------------------- Agent CRUD
def test_agents_crud_roundtrip(app_client: TestClient, pg_dsn: str) -> None:
    """POST / GET / LIST / DELETE 全链路（含 JSONB ``tools`` 往返）。

    ``PATCH`` 单独拆到 :func:`test_agents_patch_should_update_fields`：它在真实 PG 上
    会 500（见报告里的 app bug #2），不把整条 CRUD 一起拖红。
    """
    created = app_client.post(
        "/v1/agents",
        json={
            "name": "本地助手",
            "description": "集成测试用",
            "system_prompt": "你只回答本地事实。",
            "model": "mock",
            "tools": ["search", "calc"],
        },
    )
    assert created.status_code == 201, created.text
    agent = created.json()["data"]
    agent_id = agent["id"]
    assert agent_id.startswith("agt_")
    assert agent["tools"] == ["search", "calc"]
    assert agent["enabled"] is True
    assert agent["tenant_id"] == 1
    assert agent["user_id"] == "local"
    assert agent["created_at"] and agent["updated_at"]

    listed = app_client.get("/v1/agents").json()["data"]
    assert any(item["id"] == agent_id for item in listed)
    fetched = app_client.get(f"/v1/agents/{agent_id}")
    assert fetched.status_code == 200
    assert fetched.json()["data"]["name"] == "本地助手"

    # 空 PATCH 没有可改字段：不写库，直接回读（这条路不走 UPDATE，所以没被 bug #2 影响）
    unchanged = app_client.patch(f"/v1/agents/{agent_id}", json={})
    assert unchanged.status_code == 200
    assert unchanged.json()["data"]["name"] == "本地助手"

    removed = app_client.delete(f"/v1/agents/{agent_id}")
    assert removed.status_code == 200
    assert removed.json()["data"] == {"id": agent_id, "deleted": True}
    assert app_client.get(f"/v1/agents/{agent_id}").status_code == 404
    assert app_client.delete(f"/v1/agents/{agent_id}").status_code == 404
    assert run_sql(pg_dsn, "SELECT count(*) FROM local_agents WHERE id = %s", (agent_id,))[0][0] == 0


def test_agents_patch_should_update_fields(api_settings) -> None:
    """期望契约行为：``PATCH /v1/agents/{id}`` 局部更新并回读。

    现状（真实 PG 实测）：带任何字段的 PATCH 返回 500，因为 SQL 文本里出现了
    Python 列表的 repr（``['enabled = $2', 'tools = $3::jsonb']``）。修好 ``store.py:382``
    之后本测试会自动 XPASS，届时请去掉 ``xfail`` 标记。
    """
    with TestClient(create_app(api_settings), raise_server_exceptions=False) as client:
        agent = client.post("/v1/agents", json={"name": "待改", "model": "mock"}).json()["data"]
        response = client.patch(f"/v1/agents/{agent['id']}", json={"name": "改过", "tools": ["only"]})
        assert response.status_code == 200, f"PATCH 失败：{response.status_code} {response.text[:300]}"
        data = response.json()["data"]
        assert data["name"] == "改过"
        assert data["tools"] == ["only"]
        assert data["description"] == ""
        client.delete(f"/v1/agents/{agent['id']}")


def test_agents_patch_missing_id_returns_404(api_settings) -> None:
    with TestClient(create_app(api_settings), raise_server_exceptions=False) as client:
        response = client.patch("/v1/agents/agt_00000000000000000000000000000000", json={"name": "x"})
        assert response.status_code == 404, response.text


def test_agents_validation(app_client: TestClient) -> None:
    """字段校验：name 必填且有长度上限，未知字段忽略（不 500）。"""
    assert app_client.post("/v1/agents", json={}).status_code == 422
    assert app_client.post("/v1/agents", json={"name": ""}).status_code == 422
    assert app_client.post("/v1/agents", json={"name": "x" * 121}).status_code == 422
    tolerated = app_client.post("/v1/agents", json={"name": "带未知字段", "whatever": 1})
    assert tolerated.status_code == 201, tolerated.text
    app_client.delete(f"/v1/agents/{tolerated.json()['data']['id']}")


def test_chat_uses_agent_system_prompt(app_client: TestClient, mock_inference) -> None:
    """带 ``agent_id`` 提交时，Agent 的 ``system_prompt`` / ``model`` 真的进了推理请求。"""
    agent = app_client.post(
        "/v1/agents",
        json={"name": "带提示的 Agent", "system_prompt": "只回答本地事实。", "model": "mock"},
    ).json()["data"]

    mock_inference.reset()
    response = app_client.post("/v1/chat/submit", json={"message": "本地有什么", "agent_id": agent["id"]})
    assert response.status_code == 200, response.text
    assert response.json()["data"]["agent_id"] == agent["id"]

    sent = mock_inference.calls[-1]["body"]
    assert sent["messages"][0] == {"role": "system", "content": "只回答本地事实。"}
    assert sent["model"] == "mock"

    app_client.delete(f"/v1/agents/{agent['id']}")
