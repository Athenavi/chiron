"""S4 回归：子 agent 的结构化响应 schema。

三件事：

1. **解析要容忍模型的实际输出形态**（纯 JSON / ```json 包裹 / 前后带解释文字）——
   模型很少老老实实只回一个 JSON；
2. **抽取失败不阻断**：文本 `output` 照常返回，同时给出 `structured_error`；
3. **"没声明 schema"与"声明了但失败"必须能区分** —— 前者不该带任何相关字段。
"""
from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from app.agent.subagent_runner import SubAgentRunner, SubagentRunResult, _parse_json_object


class _FakeGateway:
    """只实现 `chat`（S4 抽取用非流式接口，与 `_summarise` 同一条路径）。"""

    def __init__(self, content: str = "", *, boom: bool = False) -> None:
        self._content = content
        self._boom = boom
        self.calls = 0

    async def chat(self, **kwargs: Any) -> Any:
        self.calls += 1
        if self._boom:
            raise RuntimeError("model down")
        return SimpleNamespace(content=self._content)


# ── 解析 ──────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ('{"a": 1}', {"a": 1}),
        ('```json\n{"a": 1}\n```', {"a": 1}),
        ('```\n{"a": 1}\n```', {"a": 1}),
        ('Sure, here it is:\n{"a": {"b": 2}}\nHope that helps!', {"a": {"b": 2}}),
        ("not json at all", None),
        ("[1, 2]", None),  # 合法 JSON 但不是对象
        ("", None),
        ("null", None),
    ],
)
def test_parse_json_object_tolerates_model_output(raw: str, expected: dict[str, Any] | None):
    assert _parse_json_object(raw) == expected


# ── L2 契约 ───────────────────────────────────────────────────────────────


def test_payload_distinguishes_declared_from_failed():
    """三种情况必须可分：未声明 / 成功 / 失败。"""
    base = SubagentRunResult(run_id="r", status="completed", output="x")
    payload = base.to_tool_payload()
    assert "structured" not in payload and "structured_error" not in payload

    ok = SubagentRunResult(run_id="r", status="completed", output="x", structured={"a": 1})
    assert ok.to_tool_payload()["structured"] == {"a": 1}

    failed = SubagentRunResult(
        run_id="r", status="completed", output="x", structured_error="boom"
    )
    failed_payload = failed.to_tool_payload()
    assert failed_payload["structured_error"] == "boom"
    # 失败时**仍然**有文本结果（抽取从不替换 output）
    assert failed_payload["output"] == "x"
    assert "structured" not in failed_payload


# ── 抽取 ──────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_extract_structured_success():
    gateway = _FakeGateway('{"summary": "ok", "items": [1, 2]}')
    runner = SubAgentRunner(gateway=gateway)

    structured, error = await runner._extract_structured(
        schema={"type": "object"}, output="原始文本"
    )

    assert error == ""
    assert structured == {"summary": "ok", "items": [1, 2]}
    assert gateway.calls == 1


@pytest.mark.asyncio
async def test_extract_structured_failure_is_reported_not_raised():
    """抽不出来时给出**原因**，而不是抛异常（否则整次委派会因为"格式化"失败而失败）。"""
    runner = SubAgentRunner(gateway=_FakeGateway("I cannot help with that"))

    structured, error = await runner._extract_structured(
        schema={"type": "object"}, output="原始文本"
    )

    assert structured is None
    assert "did not return a JSON object" in error


@pytest.mark.asyncio
async def test_extract_structured_survives_model_call_error():
    runner = SubAgentRunner(gateway=_FakeGateway(boom=True))

    structured, error = await runner._extract_structured(
        schema={"type": "object"}, output="原始文本"
    )

    assert structured is None
    assert "model call failed" in error


def test_runner_ignores_non_dict_schema():
    """schema 传了非 dict（模型乱填）时按"未声明"处理，而不是崩在类型上。"""
    runner = SubAgentRunner(gateway=_FakeGateway())
    assert runner._response_schema is None

    runner2 = SubAgentRunner(gateway=_FakeGateway(), response_schema={"type": "object"})
    assert runner2._response_schema == {"type": "object"}


# ── 完整 JSON Schema 校验（引入 jsonschema 之后）──────────────────────────


_SCHEMA = {
    "type": "object",
    "properties": {"summary": {"type": "string"}, "count": {"type": "integer"}},
    "required": ["summary", "count"],
}


@pytest.mark.asyncio
async def test_extract_structured_enforces_schema():
    """不合规的对象**不算**结构化结果 —— 否则调用方会以为它符合 schema。"""
    ok = SubAgentRunner(gateway=_FakeGateway('{"summary": "s", "count": 2}'))
    structured, error = await ok._extract_structured(schema=_SCHEMA, output="t")
    assert error == "" and structured == {"summary": "s", "count": 2}


@pytest.mark.asyncio
async def test_extract_structured_reports_missing_required_field():
    runner = SubAgentRunner(gateway=_FakeGateway('{"summary": "s"}'))
    structured, error = await runner._extract_structured(schema=_SCHEMA, output="t")

    assert structured is None
    assert "schema validation failed" in error
    # 错误里要指出**哪个字段**，否则调用方只能重试
    assert "count" in error


@pytest.mark.asyncio
async def test_extract_structured_reports_wrong_type():
    runner = SubAgentRunner(gateway=_FakeGateway('{"summary": "s", "count": "two"}'))
    structured, error = await runner._extract_structured(schema=_SCHEMA, output="t")

    assert structured is None
    assert "schema validation failed" in error


@pytest.mark.asyncio
async def test_extract_structured_separates_invalid_schema_from_bad_output():
    """schema 本身非法 = **调用方配置错**，必须与"模型没给对"区分开（排查方向完全不同）。"""
    runner = SubAgentRunner(gateway=_FakeGateway('{"a": 1}'))
    structured, error = await runner._extract_structured(
        schema={"type": "object", "properties": {"a": {"type": "not-a-real-type"}}},
        output="t",
    )

    assert structured is None
    assert "invalid response_schema" in error
