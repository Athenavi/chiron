"""工作流/图的归属边界（论文 §5.2 越权面：这一组端点此前只按 id 定位）。

网关代理会把 `?user_id=`（取自 JWT claims，不是客户端能写的任何位置）注入到每个转发路径，
所以这些端点**一直有**身份可用，只是从来没用它做归属判定：任意登录用户猜到/拿到一个 id
就能读别人的图定义、删别人的图、按同 id 覆盖写入、轮询到别人的执行输出。
执行链（队列 executor → 内存实例 → webhook 租户路由）的归属也在覆盖范围内。
"""

from __future__ import annotations

from typing import Any

import pytest
from fastapi import HTTPException

from app.api.workflows import (
    GraphCreateRequest,
    GraphExecuteRequest,
    create_graph,
    delete_graph,
    execute_graph,
    get_graph,
    list_graphs,
    list_instances,
    workflow_status,
)
from app.workflow.engine import NodeResult, WorkflowInstance, _instances, get_instance


class _FakePool:
    """记录每条 SQL 与参数；返回形态可配（fetchrow 命中/未命中、execute 影响行数）。"""

    def __init__(
        self,
        *,
        row: dict[str, Any] | None = None,
        rows: list[dict[str, Any]] | None = None,
        execute_result: str = "INSERT 0 1",
    ) -> None:
        self.row = row
        self.rows = rows or []
        self.execute_result = execute_result
        self.fetches: list[tuple[str, tuple]] = []
        self.fetchrows: list[tuple[str, tuple]] = []
        self.executes: list[tuple[str, tuple]] = []

    async def fetch(self, sql: str, *args: Any) -> list[dict[str, Any]]:
        self.fetches.append((sql, args))
        return self.rows

    async def fetchrow(self, sql: str, *args: Any) -> dict[str, Any] | None:
        self.fetchrows.append((sql, args))
        return self.row

    async def execute(self, sql: str, *args: Any) -> str:
        self.executes.append((sql, args))
        return self.execute_result


@pytest.fixture
def fake_pool(monkeypatch):
    holder: dict[str, _FakePool] = {}

    def _install(pool: _FakePool) -> _FakePool:
        holder["pool"] = pool
        monkeypatch.setattr("app.api.workflows.get_pool", lambda: pool)
        return pool

    return _install


# ── 身份缺失一律拒绝 ─────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_every_endpoint_requires_injected_identity(fake_pool):
    pool = fake_pool(_FakePool())
    cases = [
        list_graphs(""),
        get_graph("g-1", ""),
        delete_graph("g-1", ""),
        list_instances(""),
        workflow_status("wf_1", ""),
        create_graph(GraphCreateRequest(name="n"), ""),
        execute_graph("g-1", GraphExecuteRequest(), None, "", "t1"),
    ]
    for coro in cases:
        with pytest.raises(HTTPException) as exc:
            await coro
        assert exc.value.status_code == 403
    assert pool.fetches == [] and pool.fetchrows == [] and pool.executes == [], "先拒再查"


# ── 读：owner 条件必须写进 SQL ───────────────────────────────────────────


@pytest.mark.asyncio
async def test_graph_get_scopes_by_owner(fake_pool):
    pool = fake_pool(
        _FakePool(
            row={
                "id": "g-1",
                "name": "n",
                "user_id": "me",
                "graph_json": {"name": "n"},
                "created_at": None,
                "updated_at": None,
            }
        )
    )

    out = await get_graph("g-1", "me")

    assert out["data"]["id"] == "g-1"
    sql, args = pool.fetchrows[-1]
    assert "id = $1" in sql and "user_id = $2" in sql
    assert args == ("g-1", "me")


@pytest.mark.asyncio
async def test_graph_get_of_other_users_row_is_404(fake_pool):
    """查不到与"不是你的"给同一个答案：不透露这个 id 存在。"""
    fake_pool(_FakePool(row=None))
    with pytest.raises(HTTPException) as exc:
        await get_graph("g-1", "intruder")
    assert exc.value.status_code == 404


@pytest.mark.asyncio
async def test_graph_delete_requires_owner_match(fake_pool):
    pool = fake_pool(_FakePool(execute_result="DELETE 1"))

    out = await delete_graph("g-1", "me")

    assert out["success"] is True
    sql, args = pool.executes[-1]
    assert "DELETE FROM workflow_graphs WHERE id = $1 AND user_id = $2" in " ".join(sql.split())
    assert args == ("g-1", "me")


@pytest.mark.asyncio
async def test_graph_delete_of_other_users_row_is_404(fake_pool):
    fake_pool(_FakePool(execute_result="DELETE 0"))
    with pytest.raises(HTTPException) as exc:
        await delete_graph("g-1", "intruder")
    assert exc.value.status_code == 404


@pytest.mark.asyncio
async def test_graph_list_has_no_unscoped_branch(fake_pool):
    pool = fake_pool(_FakePool(rows=[]))

    await list_graphs("me")

    sql, args = pool.fetches[-1]
    assert "WHERE user_id::VARCHAR = $1" in sql
    assert args == ("me",)


# ── 写：不能把行归到别人名下，也不能覆盖别人的 ──────────────────────────


@pytest.mark.asyncio
async def test_create_graph_ignores_body_identity_and_scopes_update(fake_pool):
    pool = fake_pool(_FakePool())

    # `user_id` 已从请求模型移除（自报身份不算身份），旧客户端仍可能带这个键 ⇒ 被忽略
    body = GraphCreateRequest.model_validate({"name": "n", "user_id": "victim", "id": "g-1"})
    assert not hasattr(body, "user_id")

    await create_graph(body, "me")

    sql = " ".join(pool.executes[-1][0].split())
    assert "ON CONFLICT (id) DO UPDATE" in sql
    assert "WHERE workflow_graphs.user_id::VARCHAR = EXCLUDED.user_id::VARCHAR" in sql
    assert pool.executes[-1][1][2] == "me", "落库归属只认注入身份"


@pytest.mark.asyncio
async def test_create_graph_hijack_attempt_is_not_a_silent_success(fake_pool):
    """条件更新 0 行 ⇒ 既没插也没改：必须报错，不能回 200 让用户以为存好了。"""
    fake_pool(_FakePool(execute_result="INSERT 0 0"))
    with pytest.raises(HTTPException) as exc:
        await create_graph(GraphCreateRequest(name="n", id="g-1"), "me")
    assert exc.value.status_code == 409


# ── 执行与状态 ───────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_execute_graph_loads_only_own_graph(fake_pool, monkeypatch):
    pool = fake_pool(_FakePool(row={"graph_json": {"name": "n", "nodes": [], "edges": []}}))
    enqueued: list[tuple] = []

    async def _fake_enqueue(instance_id, user_id, graph_json, initial_state, tenant_id=""):  # noqa: ANN001, ANN202
        enqueued.append((instance_id, user_id, tenant_id))
        return True

    monkeypatch.setattr("app.api.workflows._enqueue_with_retry", _fake_enqueue)

    out = await execute_graph("g-1", GraphExecuteRequest(), None, "me", "t1")

    assert out["status"] == "running"
    sql, args = pool.fetchrows[0]
    assert "id = $1 AND user_id = $2" in " ".join(sql.split())
    assert args == ("g-1", "me")
    assert enqueued and enqueued[0][1] == "me" and enqueued[0][2] == "t1"
    # 落库的实例行也带归属，后续 `/status` 才认得出这是谁的
    insert_sql, insert_args = pool.executes[0]
    assert "INSERT INTO workflow_instances" in insert_sql
    assert insert_args[1] == "me"


@pytest.mark.asyncio
async def test_instance_list_scopes_by_owner(fake_pool):
    pool = fake_pool(_FakePool(rows=[]))

    await list_instances("me")

    sql, args = pool.fetches[-1]
    assert "WHERE user_id = $1" in sql
    assert args == ("me",)


@pytest.mark.asyncio
async def test_status_of_foreign_memory_instance_is_not_readable(fake_pool):
    """内存实例同样要判归属：这条路径此前**连身份参数都没有**。"""
    fake_pool(_FakePool(row=None))  # 落库也查不到 ⇒ 整体 404
    inst = WorkflowInstance(
        instance_id="wf_victim",
        graph_name="g",
        user_id="victim",
        results={"n1": NodeResult(node_id="n1", status="completed", output="secret-output")},
    )
    _instances["wf_victim"] = inst
    try:
        with pytest.raises(HTTPException) as exc:
            await workflow_status("wf_victim", "intruder")
        assert exc.value.status_code == 404
    finally:
        _instances.pop("wf_victim", None)


@pytest.mark.asyncio
async def test_status_of_own_memory_instance_returns_results(fake_pool):
    fake_pool(_FakePool())
    inst = WorkflowInstance(
        instance_id="wf_mine",
        graph_name="g",
        user_id="me",
        results={"n1": NodeResult(node_id="n1", status="completed", output="hi")},
    )
    _instances["wf_mine"] = inst
    try:
        out = await workflow_status("wf_mine", "me")
        assert out["results"]["n1"]["output"] == "hi"
    finally:
        _instances.pop("wf_mine", None)


@pytest.mark.asyncio
async def test_run_workflow_records_ownership_on_memory_instance():
    """内存实例过去从不带 user_id（字段是死的）⇒ 状态端点无从判归属。现在贯穿它。"""
    from app.workflow.engine import run_workflow

    graph = {"name": "g", "nodes": [], "edges": []}
    inst = await run_workflow(graph, None, {}, instance_id="wf_owned", user_id="me")
    try:
        assert inst.user_id == "me"
    finally:
        _instances.pop("wf_owned", None)


# ── 队列执行器：归属与租户要一路带到终态 ────────────────────────────────


@pytest.mark.asyncio
async def test_executor_emits_webhook_to_the_real_tenant(monkeypatch):
    """完成/失败事件按**租户**匹配订阅，`tenant_id=user_id` 会投给错误的收件人。"""
    from app.workflow.executor import execute_with_checkpoint

    pool = _FakePool(row={"status": "running", "checkpoint": None})
    monkeypatch.setattr("app.db.get_pool", lambda: pool)
    events: list[tuple[str, dict[str, Any], str]] = []

    async def _fake_emit(event_type: str, payload: dict[str, Any], tenant_id: str = "") -> bool:
        events.append((event_type, payload, tenant_id))
        return True

    monkeypatch.setattr("app.event_bus.emit_event", _fake_emit)

    await execute_with_checkpoint(
        "wf_exec", {"name": "g", "nodes": [], "edges": []}, {}, "u1", None, "t-real"
    )

    assert events, "终态必须发事件"
    assert events[0][2] == "t-real", "webhook 的租户路由不能被 user_id 冒充"
    assert events[0][1]["tenant_id"] == "t-real"
    inst = get_instance("wf_exec")
    assert inst is not None and inst.user_id == "u1", "队列路径的内存实例也要带归属"
    _instances.pop("wf_exec", None)
