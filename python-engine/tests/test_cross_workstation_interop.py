"""六大工作台互联互通测试。

覆盖链路: 统一入口 (/v1/chat/submit) → TaskRouter (LLM 意图/推荐站台编排)
→ 能力注册中心 (租户可见性/六大工作台能力) → 跨工作台执行 (工具上下文/依赖输出模板)
→ ContextBus 事件发布。
"""
from __future__ import annotations

import json

from httpx import ASGITransport, AsyncClient

import app.main
from app.api.unified_executor import get_chat_handler
from app.core.capabilities import (
    CapabilitiesRegistry,
    Capability,
    CapabilityType,
    WorkstationType,
    get_registry,
    preload_default_capabilities,
)
from app.core.task_router import ExecutedTask, SubTask, TaskRouter
from app.main import create_app

TENANT = "u-interop"


def _fake_gateway(intent: dict):
    """返回一个 chat_stream 恒定输出给定 JSON 的假 gateway"""
    from app.gateway.provider import ChatResponse

    payload = json.dumps(intent, ensure_ascii=False)

    class _GW:
        async def chat_stream(self, messages=None, model="", **kwargs):
            yield ChatResponse(content=payload, finish_reason="stop")

    return _GW()


# ── 能力注册中心 ───────────────────────────────────────────────────


async def test_preload_registers_all_five_workstations():
    """预加载必须覆盖六大工作台中的五大能力型工作台（dialogue 为入口不单独注册）"""
    reg = CapabilitiesRegistry()
    await preload_default_capabilities(reg)

    wst_types = {c.workstation_type for c in reg._capabilities.values()}
    assert WorkstationType.SKILL in wst_types
    assert WorkstationType.KNOWLEDGE in wst_types
    assert WorkstationType.AGENT in wst_types
    assert WorkstationType.WORKFLOW in wst_types
    assert WorkstationType.PLUGIN in wst_types

    # 全部能力必须挂真实执行器（fail loud：无执行器的能力不可执行）
    for cap in reg._capabilities.values():
        assert cap._executor is not None, f"{cap.capability_id} 缺执行器"


async def test_global_capability_visible_to_tenant():
    """全局能力（tenant_id 为空）必须对任意租户可见 — 注册中心租户过滤修复"""
    reg = CapabilitiesRegistry()
    await preload_default_capabilities(reg)

    # get_by_id 带租户参数也能找到全局能力
    cap = await reg.get_by_id("skill:read_file", tenant_id="tenant_999")
    assert cap is not None

    # search 带租户参数也能搜到全局能力
    results = await reg.search("python execute sandbox", tenant_id="tenant_999")
    assert any(c.capability_id == "skill:execute_python" for c in results)

    # list_by_workstation 同样包含全局能力
    caps = await reg.list_by_workstation(WorkstationType.AGENT, tenant_id="tenant_999")
    assert any(c.capability_id == "agent:general_chat" for c in caps)


async def test_tenant_specific_capability_isolated():
    """租户专属能力不得泄漏给其他租户"""
    reg = CapabilitiesRegistry()

    async def _noop(**kw):
        return {}

    await reg.register(Capability(
        capability_id="skill:private_tool",
        name="Private",
        description="tenant private tool",
        workstation_type=WorkstationType.SKILL,
        capability_type=CapabilityType.TOOL,
        tenant_id="tenant_a",
        _executor=_noop,
    ))

    assert await reg.get_by_id("skill:private_tool", tenant_id="tenant_a") is not None
    assert await reg.get_by_id("skill:private_tool", tenant_id="tenant_b") is None
    hits = await reg.search("private tool", tenant_id="tenant_b")
    assert hits == []


# ── TaskRouter 修复 ────────────────────────────────────────────────


async def test_llm_intent_uses_main_gateway(monkeypatch):
    """LLM 意图识别必须走 app.main.get_gateway（修复前 import 不存在的函数恒失败）"""
    intent = {
        "action": "search", "keywords": ["kb"], "entities": {},
        "complexity": "moderate", "recommended_workstations": ["agent"],
        "description": "检索", "fallback": False,
    }

    async def _get_gateway():
        return _fake_gateway(intent)

    monkeypatch.setattr(app.main, "get_gateway", _get_gateway)

    router = TaskRouter()
    result = await router._llm_understand_intent("帮我检索资料", {})
    assert result["fallback"] is False
    assert result["action"] == "search"


async def test_rule_based_search_decomposes_kb_chain():
    """search 动作必须编排 kb_list → kb_search 跨工作台链路（kb_id 模板注入）"""
    router = TaskRouter()
    subtasks = await router._rule_based_decomposition(
        action="search", keywords=["资料"], user_input="搜索相关资料",
    )
    assert [t.capability_id for t in subtasks] == ["knowledge:kb_list", "knowledge:kb_search"]
    assert subtasks[1].dependencies == ["sub_0_list"]
    assert subtasks[1].parameters["kb_id"] == "${sub_0_list.knowledge_bases.0.id}"
    assert subtasks[1].parameters["query"] == "搜索相关资料"


async def test_resolve_params_template():
    """依赖输出模板 ${dep.field.index.field} 解析"""
    router = TaskRouter()
    completed = {
        "sub_0_list": ExecutedTask(
            task_id="", subtask_id="sub_0_list", capability_id="knowledge:kb_list",
            input_params={},
            output={"count": 1, "knowledge_bases": [{"id": "kb-42", "name": "docs"}]},
        ),
    }
    resolved = router._resolve_params(
        {"kb_id": "${sub_0_list.knowledge_bases.0.id}", "query": "q"},
        completed,
    )
    assert resolved == {"kb_id": "kb-42", "query": "q"}

    # 路径不存在 → 空串（由能力参数校验显式报错）
    resolved2 = router._resolve_params({"kb_id": "${sub_0_list.nope.9}"}, completed)
    assert resolved2["kb_id"] == ""


async def test_match_falls_back_to_general_chat():
    """未匹配到能力时兜底路由到 agent:general_chat，任务链路不失联"""
    reg = get_registry()
    await preload_default_capabilities(reg)

    router = TaskRouter()
    subtasks = [SubTask(subtask_id="sub_0_default", description="随便聊聊", capability_id="")]
    matched = await router._match_capabilities(subtasks, tenant_id="tenant_x")

    assert len(matched) == 1
    assert matched[0].capability_id == "agent:general_chat"
    assert matched[0].parameters == {"task": "随便聊聊"}


async def test_recommended_workstations_pipeline(monkeypatch):
    """LLM 推荐站台 → 跨工作台线性流水线编排"""
    reg = get_registry()
    await preload_default_capabilities(reg)

    intent = {
        "action": "analyze", "keywords": ["数据"], "entities": {},
        "complexity": "complex",
        "recommended_workstations": ["agent", "knowledge", "plugin"],
        "description": "分析并检索", "fallback": False,
    }

    async def _get_gateway():
        return _fake_gateway(intent)

    monkeypatch.setattr(app.main, "get_gateway", _get_gateway)

    router = TaskRouter()
    subtasks = await router._decompose_task(intent, "tenant_x", "分析一下数据")
    assert len(subtasks) == 3
    assert subtasks[0].capability_id == "agent:general_chat"
    assert subtasks[1].capability_id == "knowledge:kb_list"
    assert subtasks[2].capability_id == "plugin:list_tools"
    # 线性依赖链：后序依赖前序
    assert subtasks[0].dependencies == []
    assert subtasks[1].dependencies == ["sub_0_agent"]
    assert subtasks[2].dependencies == ["sub_1_knowledge"]


# ── 统一入口 HTTP 路由 ─────────────────────────────────────────────


async def test_chat_submit_rejects_missing_message():
    app = create_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        resp = await ac.post("/v1/chat/submit?user_id=u1&tenant_id=t1", json={})
    assert resp.status_code == 200
    assert resp.json()["success"] is False


async def test_chat_submit_rejects_missing_tenant():
    app = create_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        # 身份由网关经 query 注入。缺 tenant_id 时应被认证层直接拒绝，
        # 而不是放行到 handler 再靠 body 里的字段兜底 —— 后者意味着
        # "没有租户身份也能到达业务逻辑"。
        resp = await ac.post(
            "/v1/chat/submit",
            json={"message": "hi"},
            params={"user_id": "u-test"},
        )
    assert resp.status_code == 401


async def test_chat_submit_cross_workstation_pipeline(monkeypatch):
    """端到端: 统一入口 → LLM 意图 → 推荐站台编排 → 跨工作台执行 → 结果聚合

    agent 工作台用假 gateway 成功执行；knowledge 工作台在无 DB 环境下
    必须显式失败（fail loud），而不是伪造成功。
    """
    reg = get_registry()
    await preload_default_capabilities(reg)

    intent = {
        "action": "analyze", "keywords": ["数据"], "entities": {},
        "complexity": "complex",
        "recommended_workstations": ["agent", "knowledge"],
        "description": "分析数据", "fallback": False,
    }

    async def _get_gateway():
        return _fake_gateway(intent)

    monkeypatch.setattr(app.main, "get_gateway", _get_gateway)

    fastapi_app = create_app()
    async with AsyncClient(transport=ASGITransport(app=fastapi_app), base_url="http://test") as ac:
        resp = await ac.post(
            "/v1/chat/submit",
            json={"message": "分析一下数据", "tenant_id": TENANT, "mode": "auto"},
            params={"user_id": "u-test", "tenant_id": TENANT},
        )

    assert resp.status_code == 200
    body = resp.json()
    assert body["success"] is True
    assert body["session_id"]
    assert body["trace_id"]

    subtasks = body["metadata"]["subtasks"]
    assert {s["capability_id"] for s in subtasks} == {"agent:general_chat", "knowledge:kb_list"}
    statuses = {s["capability_id"]: s["status"] for s in subtasks}
    assert statuses["agent:general_chat"] == "completed"
    # 无 DB 环境下知识库子任务必须显式 failed（error 透传）
    assert statuses["knowledge:kb_list"] == "failed"


async def test_capabilities_routes():
    """能力发现 API: 全量列出 + 语义搜索"""
    reg = get_registry()
    await preload_default_capabilities(reg)

    app = create_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        resp = await ac.get(
            "/v1/capabilities",
            params={"user_id": "u-test", "tenant_id": TENANT},
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["success"] is True
        assert body["count"] >= 9
        workstations = {c["workstation_type"] for c in body["capabilities"]}
        assert {"skill", "knowledge", "agent", "workflow", "plugin"} <= workstations

        resp2 = await ac.post(
            "/v1/capabilities/search",
            json={"query": "python sandbox execute", "tenant_id": TENANT},
            params={"user_id": "u-test", "tenant_id": TENANT},
        )
        assert resp2.status_code == 200
        body2 = resp2.json()
        assert body2["count"] >= 1
        assert body2["capabilities"][0]["capability_id"] == "skill:execute_python"

    # 会话消息历史路由
    get_chat_handler()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        resp3 = await ac.get(
            "/v1/chat/sessions/not-exist/messages",
            params={"user_id": "u-test", "tenant_id": TENANT},
        )
    assert resp3.json()["success"] is False
