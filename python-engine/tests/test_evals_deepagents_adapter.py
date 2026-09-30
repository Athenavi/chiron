"""E3：deepagents 适配器（**不需要**它的依赖即可测的部分）。

适配器里真正可纯函数测试的是「消息列表 → `Observation`」这一段：它决定了对标的口径是否与
Chiron 侧一致（步数、工具调用、最后一轮正文、工具错误）。用**同名假 message 类**就能测 ——
适配器靠 `type(msg).__name__` 判别消息种类，所以这里不需要 import langchain。

另一半（真调 `create_deep_agent`）只能在装了依赖的环境里跑；本文件顺带钉住"没装依赖时
必须**明确报错并给出安装提示**"，而不是抛一个看不懂的 ImportError。
"""

from __future__ import annotations

from typing import Any

import pytest

from evals.adapters import deepagents_agent as adapter
from evals.assertions import Assertion, Efficiency, evaluate

# ── 假 message（类名必须与 LangChain 一致：适配器按类名判别）────────────────


class AIMessage:
    def __init__(self, content: Any = "", tool_calls: list[dict] | None = None, usage: dict | None = None):
        self.content = content
        self.tool_calls = tool_calls or []
        self.usage_metadata = usage or {}


class ToolMessage:
    def __init__(self, content: Any = "", status: str = "success"):
        self.content = content
        self.status = status


class HumanMessage:
    def __init__(self, content: Any = ""):
        self.content = content


# ── 归约口径 ────────────────────────────────────────────────────────────


def test_steps_count_model_rounds_not_tool_calls():
    """步数 = 模型往返轮数（AIMessage 数），不是工具调用数 —— 与 Chiron 侧口径一致。"""
    messages = [
        HumanMessage("hi"),
        AIMessage("", tool_calls=[{"name": "read_file", "args": {}}, {"name": "ls", "args": {}}]),
        ToolMessage("{}"),
        ToolMessage("{}"),
        AIMessage("done"),
    ]

    obs = adapter.observation_from_messages(messages)

    assert obs.steps == 2, "两个 AIMessage = 两轮模型调用，尽管有 2 次工具调用"
    assert len(obs.tool_calls) == 2


def test_final_text_is_last_answer_with_thinking_stripped():
    messages = [
        AIMessage("[thinking]先想一下[/thinking]答案是 42"),
        AIMessage("补充一句"),
    ]

    obs = adapter.observation_from_messages(messages)

    assert obs.final_text == "答案是 42补充一句"
    assert "[thinking]" not in obs.final_text


def test_tool_error_is_recorded_from_tool_message_status():
    """工具被拒/出错是护栏证据 —— 必须从 `ToolMessage(status="error")` 落到对应调用上。"""
    messages = [
        AIMessage("", tool_calls=[{"name": "shell_exec", "args": {"cmd": "rm -rf /"}}]),
        ToolMessage("denied by policy", status="error"),
    ]

    obs = adapter.observation_from_messages(messages)

    assert obs.tool_calls[0]["error"] == "denied by policy"


def test_multimodal_content_blocks_are_joined():
    messages = [AIMessage([{"type": "text", "text": "第一段"}, {"type": "text", "text": "第二段"}])]

    obs = adapter.observation_from_messages(messages)

    assert obs.final_text == "第一段第二段"


def test_usage_metadata_is_summed():
    messages = [
        AIMessage("a", usage={"input_tokens": 100, "output_tokens": 20}),
        AIMessage("b", usage={"input_tokens": 50, "output_tokens": 10}),
    ]

    obs = adapter.observation_from_messages(messages)

    assert obs.tokens == 180


def test_empty_messages_yield_empty_observation():
    obs = adapter.observation_from_messages([])

    assert obs.steps == 0 and obs.tokens == 0 and obs.final_text == ""


def test_observation_is_directly_assertable():
    """归约结果要能直接喂给同一套断言引擎 —— 这就是"同一把尺子"。"""
    messages = [AIMessage("好的，项目说明 是标题")]

    obs = adapter.observation_from_messages(messages)
    result = evaluate(
        (Assertion(kind="final_text_contains", value="项目说明"),), Efficiency(), obs
    )

    assert result.passed is True


# ── 缺依赖的行为 ────────────────────────────────────────────────────────


def test_missing_dependency_gives_actionable_error():
    """没装对标依赖时应给出**可执行**的提示，而不是裸 ImportError。"""
    try:
        import deepagents  # noqa: F401

        pytest.skip("本机已安装 deepagents —— 这条断言只在缺依赖时有意义")
    except ImportError:
        pass

    with pytest.raises(SystemExit) as excinfo:
        adapter._import_deepagents()

    message = str(excinfo.value)
    assert "langchain" in message
    assert "pip install" in message, "要告诉人怎么装，而不是只说缺少依赖"
