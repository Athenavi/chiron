"""租户/用户级 harness 配置（P1-④）—— 读侧与接线。

这一层是 Chiron 相对 deepagents 的**超越项**：deepagents 的 harness profile 只能由部署方
在代码里注册（`register_harness_profile`），而这里下沉到租户级 / 用户级。

钉住四条边界：**默认关**（无记录 ⇒ 空串 ⇒ 前缀逐字不变）· **有上限** · **有缓存** ·
**拼接位置在稳定前缀末尾**（否则击穿前缀缓存）。
"""

from __future__ import annotations

import pytest

from app.agent import harness_store
from app.agent.runtime import AgentTask, _apply_system_prefix


def _task(*, system: str = "SYS", model: str = "gpt-4o") -> AgentTask:
    return AgentTask(
        id="t1",
        tenant_id="t1",
        user_id="u1",
        session_id="s1",
        content="hi",
        system_prompt=system,
        llm_config={"model": model},
        max_turns=2,
    )


@pytest.fixture(autouse=True)
def _clear_cache():
    harness_store.clear_cache()
    yield
    harness_store.clear_cache()  # 进程内缓存会跨用例残留


@pytest.mark.asyncio
async def test_no_tenant_yields_empty_suffix():
    """没有租户身份 ⇒ 不查库、不生效（按租户隔离的前提）。"""
    assert await harness_store.resolve_suffix("", "u1", "gpt-4o") == ""


@pytest.mark.asyncio
async def test_no_row_yields_empty_suffix(monkeypatch):
    """默认关：表里没有可用记录 ⇒ 空串。"""

    async def _none(*_args: object, **_kwargs: object) -> str:
        return ""

    monkeypatch.setattr(harness_store, "_query", _none)

    assert await harness_store.resolve_suffix("t1", "u1", "gpt-4o") == ""


@pytest.mark.asyncio
async def test_configured_suffix_is_returned(monkeypatch):
    async def _some(*_args: object, **_kwargs: object) -> str:
        return "BE TERSE"

    monkeypatch.setattr(harness_store, "_query", _some)

    assert await harness_store.resolve_suffix("t1", "u1", "gpt-4o") == "BE TERSE"


@pytest.mark.asyncio
async def test_result_is_cached_between_turns(monkeypatch):
    calls: list[tuple[object, ...]] = []

    async def _counting(*args: object, **_kwargs: object) -> str:
        calls.append(args)
        return "BE TERSE"

    monkeypatch.setattr(harness_store, "_query", _counting)

    await harness_store.resolve_suffix("t1", "u1", "gpt-4o")
    await harness_store.resolve_suffix("t1", "u1", "gpt-4o")

    assert len(calls) == 1, "低频变更、高频读取：每轮查库不值当"


def test_long_suffix_is_clamped():
    """有上限：否则一个租户能用配置吃掉所有会话的上下文预算。"""
    out = harness_store._clamp("x" * (harness_store.MAX_SUFFIX_CHARS + 500))

    assert len(out) == harness_store.MAX_SUFFIX_CHARS


def test_extra_suffix_is_appended_after_the_stable_prefix():
    msgs = _apply_system_prefix(
        [{"role": "user", "content": "hi"}], _task(), extra_suffix="EXTRA"
    )

    assert msgs[0]["role"] == "system"
    assert msgs[0]["content"].endswith("EXTRA"), "租户塑形应落在**稳定前缀的末尾**"
    assert msgs[0]["cache_breakpoint"] is True, "断点仍标在前缀上（同租户同模型下逐字稳定）"


def test_no_extra_suffix_keeps_the_prefix_identical():
    """默认关的落点：未配置时前缀必须逐字不变。"""
    without = _apply_system_prefix([{"role": "user", "content": "hi"}], _task())
    with_empty = _apply_system_prefix(
        [{"role": "user", "content": "hi"}], _task(), extra_suffix=""
    )

    assert without == with_empty
