"""A1（方案 04 批次 1）：思考走**独立事件**。

`[thinking]` 这个形态有**两个来源**：① 模型自产（`app/main.py` 的思考模式 prompt 教的）；
② 引擎包装 native reasoning（原 `runtime.py` 把 `chunk.reasoning_content` 包进 `text`）。
两者共用同一形态，消费方只能猜"这次是谁包的" —— 评测要剥、ACP 要切、前端要切、子 agent 也要切。

A1 把 native reasoning 拆成**独立事件**。本文件钉住它带来的四条后果：

1. 引擎产出 `thinking`，不再产出包装后的 `text`；
2. 评测的 `final_text` 不受思考污染（**不需要改代码**就该成立 —— 这条断言是防它退化）；
3. ACP 把 `thinking` 直接映射为 thought 增量（不再从 text 里猜）；
4. **协同的共享上下文不含思考** —— 此前它被包在 text 里混了进去（A1 顺带修好的真实污染）。

模型**自产**标记的解析是另一条通道，仍在各自消费方（前端 `splitThinking`、评测 `_strip_thinking`、
子 agent `_split_thinking`），这里不重复测。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

from app.agent.runtime import AgentRuntime, AgentTask
from app.gateway.provider import ChatResponse
from evals.assertions import Observation
from evals.observe import observation_from_events

ENGINE = Path(__file__).resolve().parents[1] / "app"


# ── 1. 引擎产出独立事件 ─────────────────────────────────────────────────


async def test_engine_emits_thinking_event_not_wrapped_text():
    gateway = MagicMock()

    async def fake_stream(**kwargs: Any):
        # 一个 chunk 同时带 reasoning 与正文 —— 正好验证两条通道互不污染
        yield ChatResponse(content="答案", reasoning_content="想了一下" * 8, finish_reason="stop")

    gateway.chat_stream = fake_stream
    runtime = AgentRuntime(gateway=gateway)
    task = AgentTask(
        id="t1",
        tenant_id="t",
        user_id="u",
        session_id="s1",
        content="hi",
        system_prompt="base",
        llm_config={"mode": "normal"},
        max_turns=1,
    )

    events = [event async for event in runtime.run(task)]

    kinds = [e.type for e in events]
    assert "thinking" in kinds, "native reasoning 应有独立事件"

    thinking = "".join(e.content for e in events if e.type == "thinking")
    assert "想了一下" in thinking, "思考内容应在 thinking 事件里"

    # 关键：思考**不再**包成 `[thinking]…[/thinking]` 混进 text
    texts = [e.content for e in events if e.type == "text"]
    assert texts, "正文仍应作为 text 事件下发"
    assert not any("[thinking]" in t for t in texts), "思考不该再混进 text"


# ── 2. 评测的 final_text 不被污染 ───────────────────────────────────────


def test_evaluation_final_text_excludes_thinking():
    """`final_text_contains` 靠这个口径 —— 思考混进去会让"想过但没说"被误判为通过。"""
    obs = observation_from_events(
        [
            {"type": "thinking", "content": "我打算说 42"},
            {"type": "text", "content": "答案是 7"},
        ]
    )

    assert obs.final_text == "答案是 7"
    assert "42" not in obs.final_text


def test_evaluation_still_strips_model_authored_markers():
    """A1 **不**取代对模型自产标记的解析 —— 那是另一条通道，必须保留。"""
    obs = observation_from_events([{"type": "text", "content": "[thinking]想[/thinking]答案"}])

    assert obs.final_text == "答案"


def test_observation_ignores_unknown_event_types():
    """thinking 事件对评测是"不认识就跳过"，不该因此报错。"""
    assert Observation().final_text == ""
    assert observation_from_events([{"type": "thinking", "content": "x"}]).steps == 0


# ── 3. ACP 直接映射 ─────────────────────────────────────────────────────


def test_acp_maps_thinking_event_to_thought_delta():
    from acp_adapter.mapping import ThoughtDelta, translate

    assert translate({"type": "thinking", "content": "想"}) == [ThoughtDelta("想")]


def test_acp_still_splits_model_authored_markers_in_text():
    from acp_adapter.mapping import TextDelta, ThoughtDelta, translate

    out = translate({"type": "text", "content": "[thinking]想[/thinking]答案"})

    assert out == [ThoughtDelta("想"), TextDelta("答案")]


# ── 4. 接线断言（子 agent 与协同）────────────────────────────────────────

# 这两条读源码而不是造一次完整委派：子 agent 的 reasoning 分派与协同的共享上下文写入都是**内联**
# 在长流程里的，单测要跑到那里需要一整套 fake gateway + store；而"有没有接线"恰好是静态可判的。
# （N1 的装配断言用了同一手法，理由相同。）


def test_subagent_runner_dispatches_thinking_events():
    src = (ENGINE / "agent" / "subagent_runner.py").read_text(encoding="utf-8")

    assert 'evt.type == "thinking"' in src, (
        "子 agent 必须认 thinking 事件，否则思考会落到 texts（L2 输出）里"
    )


def test_collaboration_shared_context_only_takes_text_events():
    """共享上下文是给**下一个 agent** 看的 —— 思考不该进去（此前它被包在 text 里混了进去）。"""
    src = (ENGINE / "agent" / "collaboration.py").read_text(encoding="utf-8")

    # 写入 shared_context 的分支必须仍然只认 text
    marker = "将输出写入共享上下文"
    assert marker in src
    tail = src[src.index(marker) : src.index(marker) + 400]
    assert 'event.type == "text"' in tail
    assert "thinking" not in tail, "thinking 不得进入 shared_context"


@pytest.mark.parametrize("path", ["agent/runtime.py"])
def test_runtime_no_longer_wraps_reasoning(path: str):
    src = (ENGINE / path).read_text(encoding="utf-8")

    assert 'type="thinking"' in src
    assert '[thinking]{safe_thinking}[/thinking]' not in src, "包装写法应已移除"
