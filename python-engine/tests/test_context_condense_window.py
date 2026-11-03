"""`/v1/context/condense` 的源消息取窗口（纯逻辑 + 假 pool，不碰真实 PG/模型）。

盯住一件事：压缩读的是**分叉点往回数的最近 N 条**，不是"会话最早的 N 条"。

为什么值得单独钉住：这个方向错了不会报错，只会让摘要变得没用 —— 超长会话里
最早的几百条往往全是开场寒暄，最近的进展被挤在 400 条之外，摘要退化成
"聊了很多但没有结论"。上下文继承要的恰恰是"现在在做什么、卡在哪"，
所以窗口必须贴着分叉点往回取，并且翻回升序交给压缩函数。
"""

from __future__ import annotations

import sys
import types
from typing import Any

import pytest

from app.api import context as ctx_mod


class _FakePool:
    """按 SQL 特征返回不同行，并记录每次调用的参数。"""

    def __init__(self, head_rows: list[dict], future_rows: list[dict] | None = None) -> None:
        self.head_rows = head_rows
        self.future_rows = future_rows or []
        self.calls: list[tuple[str, tuple[Any, ...]]] = []

    async def fetch(self, sql: str, *args: Any) -> list[dict]:
        self.calls.append((sql, args))
        if "rn > $2" in sql:
            return self.future_rows
        return self.head_rows


def _row(role: str, content: str) -> dict:
    return {"role": role, "content": content}


@pytest.fixture
def captured(monkeypatch: pytest.MonkeyPatch):
    """替换 get_pool / condense_messages / app.main.get_gateway，只留取数逻辑可观测。"""
    box: dict[str, Any] = {"messages": None, "kwargs": None}

    async def fake_condense(*, messages, gateway, keep_tail, include_future, future_messages):
        box["messages"] = messages
        box["kwargs"] = {
            "keep_tail": keep_tail,
            "include_future": include_future,
            "future_messages": future_messages,
        }
        return types.SimpleNamespace(
            summary="## 目标\n- 摘要",
            condensed=True,
            degraded=False,
            reason="",
            source_messages=len(messages),
            usage={},
        )

    fake_main = types.ModuleType("app.main")

    async def _get_gateway():
        return None

    fake_main.get_gateway = _get_gateway  # type: ignore[attr-defined]

    monkeypatch.setattr(ctx_mod, "condense_messages", fake_condense)
    monkeypatch.setitem(sys.modules, "app.main", fake_main)
    return box


def _install_pool(monkeypatch: pytest.MonkeyPatch, pool: _FakePool) -> None:
    monkeypatch.setattr(ctx_mod, "get_pool", lambda: pool)


class TestHeadWindow:
    async def test_reads_latest_before_fork_point_and_restores_ascending_order(
        self, monkeypatch: pytest.MonkeyPatch, captured
    ):
        """倒序取回"最近 N 条"，必须翻回升序再压缩。"""
        # 分叉点之前最近的 3 条，倒序返回（SQL 里是 ORDER BY rn DESC）
        pool = _FakePool([_row("user", "m9"), _row("assistant", "m8"), _row("user", "m7")])
        _install_pool(monkeypatch, pool)

        body = ctx_mod.CondenseRequest(session_id="s1", from_index=10, keep_tail=0)
        await ctx_mod.condense_context(body)

        sql, args = pool.calls[0]
        assert "rn <= $2" in sql, "必须按分叉点截断，而不是从头取"
        assert args == ("s1", 10, ctx_mod.MAX_SOURCE_MESSAGES)
        # 升序：压缩提示词按时间先后组织"目标 → 进展"，倒序喂进去会把因果讲反
        assert [m["content"] for m in captured["messages"]] == ["m7", "m8", "m9"]

    async def test_fork_point_bounds_the_window_upper_end(
        self, monkeypatch: pytest.MonkeyPatch, captured
    ):
        """`from_index` 必须进 SQL —— 否则会把分叉点之后（被裁掉）的消息也拿来压缩。"""
        pool = _FakePool([_row("user", "m2"), _row("user", "m1")])
        _install_pool(monkeypatch, pool)

        body = ctx_mod.CondenseRequest(session_id="s2", from_index=2, keep_tail=0)
        await ctx_mod.condense_context(body)

        _, args = pool.calls[0]
        assert args[1] == 2

    async def test_source_query_failure_is_400(
        self, monkeypatch: pytest.MonkeyPatch, captured
    ):
        """读不到源会话要明确 400，不能编造一份摘要。"""

        class _BoomPool:
            async def fetch(self, *_a, **_kw):
                raise RuntimeError("boom")

        monkeypatch.setattr(ctx_mod, "get_pool", lambda: _BoomPool())

        body = ctx_mod.CondenseRequest(session_id="s3", from_index=1, keep_tail=0)
        with pytest.raises(Exception) as err:
            await ctx_mod.condense_context(body)
        assert "cannot read source session" in str(err.value)


class TestFutureWindow:
    async def test_future_not_read_unless_requested(
        self, monkeypatch: pytest.MonkeyPatch, captured
    ):
        """默认只读分叉点之前 —— "不得记得未来"不变量（设计文档 2.3）。"""
        pool = _FakePool([_row("user", "m1")], future_rows=[_row("user", "m2")])
        _install_pool(monkeypatch, pool)

        body = ctx_mod.CondenseRequest(session_id="s4", from_index=1, keep_tail=0)
        await ctx_mod.condense_context(body)

        assert len(pool.calls) == 1, "没要求 include_future 就不该查后段"
        assert captured["kwargs"]["future_messages"] == []

    async def test_future_read_in_ascending_order_when_requested(
        self, monkeypatch: pytest.MonkeyPatch, captured
    ):
        """显式要求后段时，取分叉点之后的前 N 条且保持升序。"""
        pool = _FakePool(
            [_row("user", "m1")],
            future_rows=[_row("assistant", "m2"), _row("user", "m3")],
        )
        _install_pool(monkeypatch, pool)

        body = ctx_mod.CondenseRequest(
            session_id="s5", from_index=1, keep_tail=0, include_future=True
        )
        await ctx_mod.condense_context(body)

        assert len(pool.calls) == 2
        sql, args = pool.calls[1]
        assert "rn > $2" in sql
        assert args == ("s5", 1, ctx_mod.MAX_SOURCE_MESSAGES)
        assert [m["content"] for m in captured["kwargs"]["future_messages"]] == ["m2", "m3"]

    async def test_future_failure_does_not_kill_the_summary(
        self, monkeypatch: pytest.MonkeyPatch, captured
    ):
        """后段只是锦上添花：读失败要退化成纯前段摘要，而不是整个调用失败。"""

        class _HalfPool:
            def __init__(self) -> None:
                self.n = 0

            async def fetch(self, sql, *args):
                self.n += 1
                if "rn > $2" in sql:
                    raise RuntimeError("future boom")
                return [_row("user", "m1")]

        monkeypatch.setattr(ctx_mod, "get_pool", lambda: _HalfPool())

        body = ctx_mod.CondenseRequest(
            session_id="s6", from_index=1, keep_tail=0, include_future=True
        )
        out = await ctx_mod.condense_context(body)

        assert out["summary"], "后段读数失败不应让摘要整个为空"
        assert captured["kwargs"]["future_messages"] == []
