"""批 G 生命周期 hook 回归（vendor/方案03.md §3）。

对齐方案 §3 的 5 条验收：
① 默认关时零行为变化；② PreToolUse 阻断能拦住工具且审计可见；
③ hook 超时不阻断主流程；④ 多租户下用户无法注册 hook；⑤ hook 崩溃不影响引擎。
另含 fire-and-forget 事件的分派与审计身份、以及 6 个事件都有落点。

测试直接驱动 `hooks` 门面（runtime 正是这样调用它）—— hook 在**独立子进程沙箱**
里执行，因此这里也在验证"复用 plugin_runner 沙箱"这一实现要点。
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from app.config import settings
from app.hooks import OWNER_DEPLOYMENT, OWNER_USER, events, hooks

# ── 测试用 hook 脚本（必须通过 code_guard 静态守卫，故不碰 os/open 等） ──
DENY_HOOK = 'def main(input):\n    return {"decision": "deny", "reason": "blocked by test policy"}\n'
NOOP_HOOK = "def main(input):\n    return {}\n"
RAISE_HOOK = "def main(input):\n    raise RuntimeError('hook boom')\n"
WHILE_TRUE_HOOK = "def main(input):\n    while True:\n        pass\n"


@pytest.fixture(autouse=True)
def _hooks_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """每个用例：审计落盘指向 tmp、开关复位为默认关、清空注册表。

    patch `audit.AUDIT_DIR`（而不是 `settings.log_dir`）——`Settings` 是 pydantic
    模型且无该字段；同时保证任何写入都不会污染仓库的 `logs/`。
    """
    from app.hooks import audit

    monkeypatch.setattr(audit, "AUDIT_DIR", tmp_path)
    monkeypatch.setattr(settings, "hooks_enabled", False)
    monkeypatch.setattr(settings, "hooks_allow_user_defined", False)
    monkeypatch.setattr(settings, "hooks_timeout_seconds", 5)
    hooks.clear()
    yield
    hooks.clear()


def _task(**overrides: Any) -> SimpleNamespace:
    base = {"tenant_id": "t1", "user_id": "u1", "session_id": "s1"}
    base.update(overrides)
    return SimpleNamespace(**base)


def _call(name: str = "shell_exec", arguments: str = '{"cmd": "ls"}') -> dict[str, Any]:
    return {"id": "call_1", "name": name, "arguments": arguments}


def _entries(root: Path) -> list[dict[str, Any]]:
    path = root / "hooks_audit.jsonl"
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]


# ── ① 默认关时零行为变化 ─────────────────────────────────────────────


async def test_default_off_zero_behavior_change(tmp_path: Path) -> None:
    """默认关：即使已注册"必然阻断"的 hook，也不执行、无任何审计。"""
    assert hooks.register(events.PRE_TOOL_USE, "deny_all", DENY_HOOK) is True
    assert await hooks.before_tool_use(task=_task(), tool_call=_call()) is None
    assert _entries(tmp_path) == [], "默认关时不应有任何 hook 执行痕迹"

    # fire-and-forget 事件同样空操作
    await hooks.session_start(task=_task())
    await hooks.after_tool_use(task=_task(), tool_call=_call(), result={})
    await hooks.stop(task=_task())
    assert _entries(tmp_path) == []


# ── ② PreToolUse 阻断能拦住工具且审计可见 ────────────────────────────


async def test_pre_tool_use_blocks_and_is_audited(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "hooks_enabled", True)
    hooks.register(events.PRE_TOOL_USE, "deny_rm", DENY_HOOK)

    blocked = await hooks.before_tool_use(task=_task(), tool_call=_call())
    assert blocked is not None and "deny_rm" in blocked

    entries = _entries(tmp_path)
    assert entries, "阻断必须留审计"
    last = entries[-1]
    assert last["event"] == events.PRE_TOOL_USE
    assert last["blocked"] is True
    assert last["hook"] == "deny_rm"
    # 身份齐全：谁、哪个租户
    assert last["tenant"] == "t1"
    assert last["user"] == "u1"
    assert last["session"] == "s1"
    # 耗时与退出码
    assert "duration_ms" in last
    assert last["exit_code"] is not None


async def test_blocked_reason_is_returned_as_tool_error(monkeypatch: pytest.MonkeyPatch) -> None:
    """注入防护：hook 的返回只作为**工具错误**回灌（不进入 system prompt）。"""
    monkeypatch.setattr(settings, "hooks_enabled", True)
    hooks.register(events.PRE_TOOL_USE, "deny_html", DENY_HOOK)

    blocked = await hooks.before_tool_use(task=_task(), tool_call=_call())
    assert blocked is not None
    assert blocked.startswith("blocked by PreToolUse hook")


async def test_pre_tool_use_allow_does_not_block(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """hook 正常返回但未 deny → 放行（默认语义是 allow，不是 deny）。"""
    monkeypatch.setattr(settings, "hooks_enabled", True)
    hooks.register(events.PRE_TOOL_USE, "ok_hook", NOOP_HOOK)

    assert await hooks.before_tool_use(task=_task(), tool_call=_call()) is None
    assert _entries(tmp_path)[-1]["blocked"] is False


# ── ③ hook 超时不阻断主流程 ──────────────────────────────────────────


async def test_hook_timeout_does_not_block(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "hooks_enabled", True)
    monkeypatch.setattr(settings, "hooks_timeout_seconds", 1)
    hooks.register(events.PRE_TOOL_USE, "slow", WHILE_TRUE_HOOK)

    assert await hooks.before_tool_use(task=_task(), tool_call=_call()) is None, "超时必须放行"
    last = _entries(tmp_path)[-1]
    assert last["outcome"] == "timeout"
    assert last["blocked"] is False


# ── ④ 多租户下用户无法注册 hook ──────────────────────────────────────


async def test_user_cannot_register_hook_by_default(tmp_path: Path) -> None:
    """默认 `hooks_allow_user_defined=false`：用户注册被拒绝，且留审计。"""
    assert hooks.register(events.PRE_TOOL_USE, "u_deny", DENY_HOOK, owner=OWNER_USER) is False
    assert hooks.registry.for_event(events.PRE_TOOL_USE) == []

    entries = _entries(tmp_path)
    assert entries and entries[-1]["outcome"] == "register_denied"
    assert entries[-1]["owner"] == OWNER_USER


async def test_user_can_register_only_when_explicitly_allowed(monkeypatch: pytest.MonkeyPatch) -> None:
    """对照：显式开放 + 开启机制后，用户自定义 hook 才生效。"""
    monkeypatch.setattr(settings, "hooks_allow_user_defined", True)
    monkeypatch.setattr(settings, "hooks_enabled", True)

    assert hooks.register(events.PRE_TOOL_USE, "u_deny", DENY_HOOK, owner=OWNER_USER) is True
    assert await hooks.before_tool_use(task=_task(), tool_call=_call()) is not None


async def test_unknown_event_is_rejected() -> None:
    assert hooks.register("NotAnEvent", "x", NOOP_HOOK) is False


# ── ⑤ hook 崩溃不影响引擎 ────────────────────────────────────────────


async def test_hook_crash_does_not_break_engine(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "hooks_enabled", True)
    hooks.register(events.PRE_TOOL_USE, "boom", RAISE_HOOK)

    # 崩溃的 PreToolUse 既不阻断、也不抛异常
    assert await hooks.before_tool_use(task=_task(), tool_call=_call()) is None
    assert _entries(tmp_path)[-1]["outcome"] == "error"

    # fire-and-forget 事件崩溃同样只是被吞掉
    hooks.clear()
    hooks.register(events.POST_TOOL_USE, "boom2", RAISE_HOOK)
    await hooks.after_tool_use(task=_task(), tool_call=_call(), result={"ok": True})
    assert _entries(tmp_path)[-1]["outcome"] == "error"


# ── 补充：事件分派、6 事件落点、启动告警 ─────────────────────────────


async def test_after_tool_use_routes_to_failure_event(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """收尾事件按结果成功/失败分派：失败进 PostToolUseFailure，成功进 PostToolUse。"""
    monkeypatch.setattr(settings, "hooks_enabled", True)
    hooks.register(events.POST_TOOL_USE, "ok_hook", NOOP_HOOK)
    hooks.register(events.POST_TOOL_USE_FAILURE, "fail_hook", NOOP_HOOK)

    await hooks.after_tool_use(task=_task(), tool_call=_call(), result={"error": "boom"})
    seen = [entry["event"] for entry in _entries(tmp_path)]
    assert events.POST_TOOL_USE_FAILURE in seen
    assert events.POST_TOOL_USE not in seen

    await hooks.after_tool_use(task=_task(), tool_call=_call(), result={"output": "fine"})
    assert _entries(tmp_path)[-1]["event"] == events.POST_TOOL_USE


async def test_all_six_events_have_a_call_site(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """6 个引擎侧事件都能被触发（SessionStart/Stop 与 Subagent 对都要有落点）。"""
    monkeypatch.setattr(settings, "hooks_enabled", True)
    for event in (
        events.SESSION_START,
        events.STOP,
        events.SUBAGENT_START,
        events.SUBAGENT_STOP,
    ):
        hooks.register(event, f"h_{event}", NOOP_HOOK)

    await hooks.session_start(task=_task())
    await hooks.subagent_start(task=_task(), tool_call=_call("subagent"))
    await hooks.subagent_stop(task=_task(), tool_call=_call("subagent"), result={})
    await hooks.stop(task=_task())

    seen = {entry["event"] for entry in _entries(tmp_path)}
    assert {
        events.SESSION_START,
        events.STOP,
        events.SUBAGENT_START,
        events.SUBAGENT_STOP,
    } <= seen


async def test_sandbox_blocks_dangerous_hook(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """复用 plugin_runner 沙箱：hook 试图 import os 被静态守卫拦下（成功=false）。"""
    monkeypatch.setattr(settings, "hooks_enabled", True)
    hooks.register(events.PRE_TOOL_USE, "evil", "import os\n\ndef main(input):\n    return os.getpid()\n")

    # 被沙箱拦下 → 视为失败 → 不阻断
    assert await hooks.before_tool_use(task=_task(), tool_call=_call()) is None
    assert _entries(tmp_path)[-1]["outcome"] == "error"


def test_startup_warning_when_user_hooks_enabled(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """用户自定义 hook 启用时给出显著启动告警 + 审计（SaaS 误开必须可见）。"""
    from app.hooks import warn_if_user_defined_enabled

    monkeypatch.setattr(settings, "hooks_enabled", True)
    monkeypatch.setattr(settings, "hooks_allow_user_defined", True)
    with caplog.at_level(logging.WARNING):
        warn_if_user_defined_enabled()

    assert any("user-defined" in rec.getMessage().lower() for rec in caplog.records)
    assert _entries(tmp_path)[-1]["outcome"] == "user_hooks_enabled"


def test_deployment_owner_is_default() -> None:
    hooks.register(events.STOP, "d", NOOP_HOOK)
    assert hooks.registry.all()[0].owner == OWNER_DEPLOYMENT
