"""跨链路 workbench_context 契约测试。

背景：前端 ``ChatView.buildContext`` 只产出一份 context，却被送到两条后端链路：

- **SSE 链路**：``POST /submit`` → Go 网关 → Python ``/v1/agent/submit`` → ``AgentRuntime``
- **统一链路**：``POST /v1/chat/submit`` → ``submit_chat`` → ``UnifiedExecutor``

两条链路各自解析那份 context，历史上因此分叉：同一个 ``workflow_id`` 在统一链路会被
执行（``_execute_via_workflow``），在 SSE 链路却没有任何消费点。用户症状是"在工作流页
点『在对话中使用』完全没反应" —— 无报错、无日志，工作流就是没被使用。

这组测试锁死两件事：

1. 关联目标的读取只有一处实现（``app/agent/workbench_context.py``）；
2. SSE 链路确实消费 workflow 关联目标。

为什么不用"两条链路各调一次、断言结果相同"来测：那样在两边共用同一函数时是恒真断言，
拦不住任何东西。真正会漂移的是"某条链路又自己写一遍解析"，所以这里用实现级守卫
（断言两条链路都经由共享函数）来兜住它。
"""

from __future__ import annotations

import inspect
import json

import pytest

from app.agent.workbench_context import selected_workflow_ids
from tests.source_scan import without_comments


class FakeRequest:
    """只实现 agent_submit 用到的 ``.json()``，避免为测试拉起整个 ASGI 栈。"""

    def __init__(self, body: dict):
        self._body = body

    async def json(self) -> dict:
        return self._body


async def _collect_sse(response) -> str:
    """把 StreamingResponse 的 SSE 负载拼成一段文本，供断言检索。"""
    chunks = []
    async for chunk in response.body_iterator:
        chunks.append(chunk.decode() if isinstance(chunk, bytes) else chunk)
    return "".join(chunks)


def _events_of(payload: str) -> list[dict]:
    """从 SSE 文本里取出 data 行并解析为事件对象。"""
    out = []
    for line in payload.splitlines():
        if line.startswith("data: "):
            out.append(json.loads(line[len("data: ") :]))
    return out


@pytest.fixture
def fake_workflow_runner(monkeypatch):
    """替换工作流载入与执行，让 SSE 链路测试不触碰 DB 与 LLM gateway。

    返回 spy 字典：``loaded`` 记录 load 收到的 id 列表，``ran`` 记录执行调用。
    """
    from app.api import unified_executor

    spy = {"loaded": [], "ran": []}

    async def fake_load(workflow_ids):
        spy["loaded"].append(list(workflow_ids))
        return [("wf-1", {"nodes": [{"id": "n1"}], "edges": []})]

    async def fake_run(graphs, user_input, trace_id, gateway):
        spy["ran"].append({"graphs": graphs, "user_input": user_input})
        return {
            "status": "completed",
            "output": {"result": f"workflow-handled:{user_input}"},
            "workflow_ids": [graph_id for graph_id, _ in graphs],
        }

    monkeypatch.setattr(unified_executor, "load_selected_workflows", fake_load)
    monkeypatch.setattr(unified_executor, "run_workflow_graphs", fake_run)
    return spy


class TestSelectedWorkflowIds:
    """共享解析函数的读取口径（多值优先 / 单值回退 / 去空去重）"""

    def test_prefers_multi_value(self):
        assert selected_workflow_ids({"workflow_ids": ["a", "b"], "workflow_id": "z"}) == ["a", "b"]

    def test_falls_back_to_single_value(self):
        # 前端从工作流页跳转只发单值（?workflow=<id> → workflow_id）
        assert selected_workflow_ids({"workflow_id": "only"}) == ["only"]

    def test_absent_context_yields_nothing_to_run(self):
        assert selected_workflow_ids({}) == []
        assert selected_workflow_ids(None) == []
        assert selected_workflow_ids("not-a-dict") == []

    def test_trims_and_dedupes(self):
        assert selected_workflow_ids({"workflow_ids": ["  x  ", "", "x", "y"]}) == ["x", "y"]

    def test_ignores_other_workstations(self):
        # kb / agent 的关联目标不该混进工作流执行清单
        ctx = {"kb_ids": ["k1"], "agent_ids": ["a1"], "workflow_ids": ["w1"]}
        assert selected_workflow_ids(ctx) == ["w1"]

    def test_kb_only_context_has_no_workflow_target(self):
        """只带知识库时不产生工作流目标 —— SSE 链路据此整体跳过工作流分支。"""
        assert selected_workflow_ids({"kb_id": "k1"}) == []
        assert selected_workflow_ids({"kb_ids": ["k1", "k2"], "skill_names": ["pdf"]}) == []


class TestSseLinkConsumesWorkflow:
    """SSE 链路：带着工作流进对话时，工作流必须真的被执行。"""

    @pytest.mark.asyncio
    async def test_single_workflow_id_is_executed(self, fake_workflow_runner):
        from app.main import agent_submit

        body = {
            "content": "分析这份报告",
            "tenant_id": "t1",
            "session_id": "s1",
            "context": {"workflow_id": "wf-1"},
        }
        response = await agent_submit(FakeRequest(body), gateway=None)
        payload = await _collect_sse(response)

        assert fake_workflow_runner["loaded"] == [["wf-1"]]
        assert fake_workflow_runner["ran"][0]["user_input"] == "分析这份报告"
        assert "workflow-handled:分析这份报告" in payload

    @pytest.mark.asyncio
    async def test_multi_value_is_a_pipeline_in_selection_order(self, fake_workflow_runner):
        from app.main import agent_submit

        body = {
            "content": "继续",
            "context": {"workflow_ids": ["wf-1", "wf-2"]},
        }
        await agent_submit(FakeRequest(body), gateway=None)

        assert fake_workflow_runner["loaded"] == [["wf-1", "wf-2"]]

    @pytest.mark.asyncio
    async def test_result_is_emitted_as_text_event(self, fake_workflow_runner):
        """结果必须以 text 事件下发 —— Go 网关只对 text 事件累加最终内容并落库，
        换成别的类型会表现为"对话里看见了、刷新就没了"。"""
        from app.main import agent_submit

        body = {"content": "hi", "context": {"workflow_id": "wf-1"}}
        response = await agent_submit(FakeRequest(body), gateway=None)
        events = _events_of(await _collect_sse(response))

        texts = [e for e in events if e.get("type") == "text"]
        assert any(e.get("content") == "workflow-handled:hi" for e in texts)


class TestNoForkedParsing:
    """实现级守卫：两条链路都必须经由共享解析函数，不得各自再写一遍。

    **判据（2026-10-09 加固）**：`inspect.getsource` 拿到的函数源码里**既有 import 行也有调用**，
    所以只断言"名字出现过"是不够的 —— ① 把调用**注释掉**时子串仍在（实测旧判据 exit=0）；
    ② `agent_submit` 里的 `from … import selected_workflow_ids` 本身就含这个名字。
    因此：**先剥注释**（`tests/source_scan`）**再要求"调用形态"** `selected_workflow_ids(`。
    """

    def test_unified_link_uses_shared_parser(self):
        from app.api.unified_executor import UnifiedChatHandler

        source = without_comments(inspect.getsource(UnifiedChatHandler.submit_task))
        assert "selected_workflow_ids(" in source, (
            "统一链路又自己解析 workflow 关联目标了 —— 请改用 "
            "app/agent/workbench_context.py 的 selected_workflow_ids"
        )

    def test_sse_link_uses_shared_parser(self):
        from app.main import agent_submit

        source = without_comments(inspect.getsource(agent_submit))
        assert "selected_workflow_ids(" in source, (
            "SSE 链路没有接入共享解析函数 —— 这正是 workflow_id 在 SSE 链路"
            "被静默丢弃的原因"
        )
