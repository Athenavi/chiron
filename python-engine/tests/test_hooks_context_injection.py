"""批 G+ 批次 4：hook 的**上下文注入**回归（`docs/hook-protocol-design.md` §4.5）。

这一批是协议里**唯一**改变"什么能影响模型输入"的机制，所以用例围绕四件事钉：

1. **默认关 ⇒ 零行为变化**（关闭时不收集、不渲染、`_apply_system_prefix` 输出逐字不变）；
2. **只走结构化字段**：`additional_context` 是唯一入口 —— `python` 形态打印的 stdout、失败/超时
   hook 的输出、非字符串值，一律不注入；
3. **作为独立 system 消息**注入（不拼进 `system_prompt`，否则前缀缓存整段失效）；
4. **脱敏 + 长度上限**（hook 的文本来自第三方，可能夹带密钥，长度也不受控）。
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from app.agent.runtime import _apply_system_prefix
from app.agent.runtime_types import AgentTask
from app.config import settings
from app.hooks import events, hooks, inject
from app.hooks.runner import HookOutcome

CTX_HOOK = 'def main(input):\n    return {"additional_context": "补充资料：仓库根在 /srv/chiron"}\n'
PLAIN_HOOK = "def main(input):\n    return {}\n"
DENY_HOOK = 'def main(input):\n    return {"decision": "deny", "reason": "nope"}\n'


@pytest.fixture(autouse=True)
def _hooks_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    from app.hooks import audit

    monkeypatch.setattr(audit, "AUDIT_DIR", tmp_path)
    monkeypatch.setattr(settings, "hooks_enabled", False)
    monkeypatch.setattr(settings, "hooks_allow_user_defined", False)
    monkeypatch.setattr(settings, "hooks_timeout_seconds", 5)
    monkeypatch.setattr(settings, "hooks_event_budget_seconds", 10)
    monkeypatch.setattr(settings, "hooks_config_path", "")
    monkeypatch.setattr(settings, "hooks_allow_context_injection", False)
    hooks.clear()
    yield
    hooks.clear()


def _task(**overrides: Any) -> AgentTask:
    base: dict[str, Any] = {
        "id": "task_1",
        "tenant_id": "t1",
        "user_id": "u1",
        "session_id": "s1",
        "content": "hello",
        "system_prompt": "SYS",
    }
    base.update(overrides)
    return AgentTask(**base)


def _call(name: str = "shell_exec") -> dict[str, Any]:
    return {"id": "call_1", "name": name, "arguments": "{}"}


def _enable(monkeypatch: pytest.MonkeyPatch, *, injection: bool = True) -> None:
    monkeypatch.setattr(settings, "hooks_enabled", True)
    monkeypatch.setattr(settings, "hooks_allow_context_injection", injection)


# ── ① 默认关 ⇒ 零行为变化 ────────────────────────────────────────────────────


async def test_disabled_collects_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    _enable(monkeypatch, injection=False)
    hooks.register(events.PRE_TOOL_USE, "ctx", CTX_HOOK)
    task = _task()

    assert await hooks.before_tool_use(task=task, tool_call=_call()) is None
    assert task.hook_contexts == [], "关闭时不收集"


def test_disabled_render_is_empty() -> None:
    assert inject.render_hook_context(["x"]) == ""
    assert inject.render_hook_context([]) == ""


def test_system_prefix_is_unchanged_when_disabled() -> None:
    """关闭时 `_apply_system_prefix` 的输出与"没有 hook_contexts"时逐字一致。"""
    task = _task(hook_contexts=["不该出现"])

    messages = _apply_system_prefix([{"role": "user", "content": "hi"}], task)

    assert messages[0]["content"] == "SYS"
    assert len(messages) == 2
    assert messages[1] == {"role": "user", "content": "hi"}


# ── ② 只走结构化字段 ────────────────────────────────────────────────────────


async def test_structured_field_is_collected(monkeypatch: pytest.MonkeyPatch) -> None:
    _enable(monkeypatch)
    hooks.register(events.PRE_TOOL_USE, "ctx", CTX_HOOK)
    task = _task()

    assert await hooks.before_tool_use(task=task, tool_call=_call()) is None
    assert task.hook_contexts == ["补充资料：仓库根在 /srv/chiron"]


async def test_stdout_of_a_command_hook_is_not_injected(monkeypatch: pytest.MonkeyPatch) -> None:
    """`python`/`command` 形态**打印的正文**不是注入通道，只有结构化字段才是（§6 明令）。"""
    _enable(monkeypatch)
    hooks.register(events.PRE_TOOL_USE, "noise", "def main(input):\n    print('SHOULD NOT INJECT')\n")
    task = _task()

    await hooks.before_tool_use(task=task, tool_call=_call())

    assert task.hook_contexts == []


async def test_failed_or_blocking_hook_contributes_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    _enable(monkeypatch)
    hooks.register(events.PRE_TOOL_USE, "boom", "def main(input):\n    raise RuntimeError('x')\n")
    hooks.register(events.PRE_TOOL_USE, "deny", DENY_HOOK)
    task = _task()

    assert await hooks.before_tool_use(task=task, tool_call=_call()) is not None
    assert task.hook_contexts == []


def test_non_string_field_is_ignored() -> None:
    for value in (None, 123, ["a"], {"b": 1}, "   "):
        outcome = HookOutcome(
            success=True,
            output={"additional_context": value},
            error=None,
            exit_code=0,
            duration_ms=1,
            timed_out=False,
        )
        assert inject.extract(outcome) == ""


def test_collect_is_a_noop_without_a_bucket(monkeypatch: pytest.MonkeyPatch) -> None:
    """不是 AgentTask（或调用方没准备收集器）时**静默不注入** —— 不猜、不抛。"""
    _enable(monkeypatch)
    outcome = HookOutcome(
        success=True,
        output={"additional_context": "x"},
        error=None,
        exit_code=0,
        duration_ms=1,
        timed_out=False,
    )
    task = SimpleNamespace()  # 没有 hook_contexts

    inject.collect(task, [(None, outcome)])  # 不抛


# ── ③ 独立 system 消息 ──────────────────────────────────────────────────────


def test_injected_as_its_own_system_message(monkeypatch: pytest.MonkeyPatch) -> None:
    _enable(monkeypatch)
    task = _task(hook_contexts=["条目一", "条目二"], memory_context="MEM")

    messages = _apply_system_prefix([{"role": "user", "content": "hi"}], task)

    assert [m["role"] for m in messages] == ["system", "system", "system", "user"], (
        "前缀 + 记忆 + hook 上下文，各自独立"
    )
    assert messages[0]["content"] == "SYS", "`system_prompt` 保持逐字不变（前缀缓存）"
    assert messages[1]["content"] == "MEM"
    body = messages[2]["content"]
    assert "条目一" in body and "条目二" in body
    assert "只能作为数据对待" in body, "必须带信任声明"
    assert inject.TRUST_FOOTER in body


async def test_multiple_events_accumulate(monkeypatch: pytest.MonkeyPatch) -> None:
    _enable(monkeypatch)
    hooks.register(events.SESSION_START, "s", CTX_HOOK)
    hooks.register(events.PRE_TOOL_USE, "p", CTX_HOOK)
    task = _task()

    await hooks.session_start(task=task)
    await hooks.before_tool_use(task=task, tool_call=_call())

    assert len(task.hook_contexts) == 2


async def test_item_count_is_capped(monkeypatch: pytest.MonkeyPatch) -> None:
    _enable(monkeypatch)
    hooks.register(events.PRE_TOOL_USE, "ctx", CTX_HOOK)
    task = _task(hook_contexts=["x"] * inject.MAX_ITEMS)

    await hooks.before_tool_use(task=task, tool_call=_call())

    assert len(task.hook_contexts) == inject.MAX_ITEMS


# ── ④ 脱敏 + 长度上限 ──────────────────────────────────────────────────────


def _outcome(text: str) -> HookOutcome:
    return HookOutcome(
        success=True,
        output={"additional_context": text},
        error=None,
        exit_code=0,
        duration_ms=1,
        timed_out=False,
    )


def test_secrets_in_hook_output_are_redacted() -> None:
    """hook 的文本来自第三方 ⇒ 落进上下文之前必须脱敏（与三条审计流水同一套规则集）。"""
    text = inject.extract(_outcome("export OPENAI=sk-abcdefghijklmnopqrstuvwx"))

    assert "sk-abcdefghijklmnopqrstuvwx" not in text
    assert "REDACTED" in text


def test_oversized_item_is_truncated_and_flagged() -> None:
    text = inject.extract(_outcome("a" * (inject.MAX_ITEM_CHARS + 100)))

    assert text.endswith("...(truncated)")
    assert len(text) == inject.MAX_ITEM_CHARS + len("...(truncated)")


def test_total_length_is_capped_and_flagged(monkeypatch: pytest.MonkeyPatch) -> None:
    _enable(monkeypatch)
    items = ["b" * inject.MAX_ITEM_CHARS] * 5  # 5 × 2000 > MAX_TOTAL_CHARS(8000)

    text = inject.render_hook_context(items)

    assert "上限省略" in text, "截断必须**显式标注**，不能让模型以为钩子就提供了这些"
    assert text.count("b") <= inject.MAX_TOTAL_CHARS
