"""差距C（执行图）：委派这一跳的 span。

改动前 `record_span` 在 `subagent_runner.py` 里出现 **0 次** —— runtime 给 run / turn / llm /
tool 都打了点，唯独「父 → 子」这一跳没有，于是执行图里子 Agent 那一支根本不存在，
"某次委派从哪来"只能翻 JSON 日志。

本文件锁住四件事（前三条是能接上的前提，第四条是安全底线）：

1. 节点 id 由现有标识（父 trace_id + run_id）算出来，可重放、不产生重影节点；
2. 父指针取自工具上下文（`AgentRuntime.run` 写入），指向**父 turn** 而不是父 run 根；
3. 没有父语境时**整条打点关闭**（不凭空造 trace —— 那只会产出父图里看不见的孤儿节点）；
4. 打点抛异常时**绝不**向调用方传播（委派不能因为观测面出问题而失败）。
"""

from __future__ import annotations

import pytest

from app.agent import subagent_runner as runner_mod


class TestDelegationSpanIds:
    """节点 id 是纯函数算出来的 —— 可重放，不依赖随机数。"""

    def test_terminal_and_dispatch_ids_differ(self):
        base, spawn = runner_mod._delegation_span_ids("tr_abc", "rs_1")

        assert base == "tr_abc:subagent:rs_1"
        assert spawn == "tr_abc:subagent:rs_1:spawn"
        # 两个 id 必须不同：相同就会在图上互相覆盖，"派发"与"终态"只剩一条。
        assert base != spawn

    def test_ids_are_stable_across_calls(self):
        """同一 (trace, run) 反复算必须得到同一结果 —— 否则补写/重放会产生重影节点。"""
        assert runner_mod._delegation_span_ids("tr_abc", "rs_1") == runner_mod._delegation_span_ids(
            "tr_abc", "rs_1"
        )


class TestParentTraceContext:
    """父指针来自工具上下文，取不到必须是空串（而不是编一个值）。"""

    def test_absent_context_yields_empty_strings(self, monkeypatch):
        from app.tools import context as ctx_mod

        monkeypatch.setattr(ctx_mod, "get_tool_context", lambda key, default=None: default)

        assert runner_mod._parent_trace_context() == ("", "")

    def test_reads_trace_and_turn_span(self, monkeypatch):
        from app.tools import context as ctx_mod

        values = {"trace_id": "tr_abc", "span_id": "tr_abc:turn:2"}
        monkeypatch.setattr(
            ctx_mod, "get_tool_context", lambda key, default=None: values.get(key, default)
        )

        assert runner_mod._parent_trace_context() == ("tr_abc", "tr_abc:turn:2")


class TestRecordDelegationSpan:
    """打点的两条守卫：无父 trace 则关闭；抛异常则吞掉。"""

    @pytest.mark.asyncio
    async def test_no_trace_id_is_a_noop(self, monkeypatch):
        calls: list[dict] = []

        async def _fake_record_span(**kwargs):
            calls.append(kwargs)

        monkeypatch.setattr("app.trace.record_span", _fake_record_span, raising=False)

        await runner_mod._record_delegation_span(
            tenant_id=1,
            trace_id="",  # ← 没有父 trace
            span_id="x",
            parent_span_id="y",
            span_name="subagent",
            duration_ms=1,
            metadata={},
        )

        assert calls == [], "没有父 trace 时必须一条都不写（否则产出孤儿节点）"

    @pytest.mark.asyncio
    async def test_passes_parent_pointer_and_kind(self, monkeypatch):
        calls: list[dict] = []

        async def _fake_record_span(**kwargs):
            calls.append(kwargs)

        monkeypatch.setattr("app.trace.record_span", _fake_record_span, raising=False)

        await runner_mod._record_delegation_span(
            tenant_id=7,
            trace_id="tr_abc",
            span_id="tr_abc:subagent:rs_1",
            parent_span_id="tr_abc:turn:2",
            span_name="subagent",
            duration_ms=42,
            metadata={"run_id": "rs_1"},
        )

        assert len(calls) == 1
        span = calls[0]
        # 父指针是三要素里唯一"凭空补不上"的那个 —— 必须原样透传。
        assert span["parent_span_id"] == "tr_abc:turn:2"
        assert span["span_id"] == "tr_abc:subagent:rs_1"
        assert span["kind"] == runner_mod.SUBAGENT_SPAN_KIND
        assert span["trace_id"] == "tr_abc"
        assert span["duration_ms"] == 42

    @pytest.mark.asyncio
    async def test_negative_duration_is_clamped(self, monkeypatch):
        calls: list[dict] = []

        async def _fake_record_span(**kwargs):
            calls.append(kwargs)

        monkeypatch.setattr("app.trace.record_span", _fake_record_span, raising=False)

        await runner_mod._record_delegation_span(
            tenant_id=1,
            trace_id="tr_abc",
            span_id="s",
            parent_span_id="p",
            span_name="subagent",
            duration_ms=-5,
            metadata={},
        )

        assert calls[0]["duration_ms"] == 0

    @pytest.mark.asyncio
    async def test_observability_failure_never_escapes(self, monkeypatch):
        """trace 模块坏了（Redis 配置错误直接抛）也不能让委派失败。"""

        async def _boom(**kwargs):
            raise RuntimeError("redis exploded")

        monkeypatch.setattr("app.trace.record_span", _boom, raising=False)

        # 不抛异常就是全部要求：调用方（SubAgentRunner）不该被观测面拖下水。
        await runner_mod._record_delegation_span(
            tenant_id=1,
            trace_id="tr_abc",
            span_id="s",
            parent_span_id="p",
            span_name="subagent",
            duration_ms=1,
            metadata={},
        )
