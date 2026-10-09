"""批 G+ 批次 5 · `ask` 档（hook **要求用户确认**）回归。

2026-10-09 拍板：`ask` **要做**，形态是**复用既有的服务端审批通道**（不新增交互面 ——
不新增端点、不新造一套等待语义）。本文件钉五件事：

1. `ask` ⇒ **不执行工具**，而是走审批（与 `tool_policy` 的 `confirm` **同一套**手续）；
2. 批准后再执行时 **不再询问**（否则用户批一次、hook 问一次 ⇒ 无限循环）；
3. `deny` 仍然生效（hook 只能收紧，**不会**被"批准过"抵消）；
4. **默认关 ⇒ 零行为变化**；
5. 并发批（**不能交互**）里遇到 `ask` ⇒ **显式错误**，而不是把审批事件静默丢掉。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from app.agent.runtime import AgentRuntime
from app.agent.runtime_types import AgentTask
from app.config import settings
from app.hooks import DECISION_ASK, DECISION_DENY, events, hooks

#: 要求用户确认（不拒绝）
ASK_HOOK = (
    "def main(input):\n"
    '    return {"decision": "ask", "reason": "ops wants a human to confirm"}\n'
)
#: 直接拒绝
DENY_HOOK = (
    "def main(input):\n"
    '    return {"decision": "deny", "reason": "ops forbids this"}\n'
)

#: `read_file` 在工具策略里是**读级 / allow** —— 用它才能证明"审批是 hook 要来的"，
#: 而不是 `tool_policy` 本来就判了 `confirm`（那是另一条路径，见 test_runtime.py）。
READ_TOOL = "read_file"


@pytest.fixture(autouse=True)
def _hooks_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """审计落盘指向 tmp（两条流水都要）、开关复位为默认关、清空注册表。"""
    from app.agent import approval_audit
    from app.hooks import audit

    monkeypatch.setattr(audit, "AUDIT_DIR", tmp_path)
    monkeypatch.setattr(approval_audit, "AUDIT_DIR", tmp_path)
    monkeypatch.setattr(settings, "hooks_enabled", False)
    monkeypatch.setattr(settings, "hooks_timeout_seconds", 5)
    monkeypatch.setattr(settings, "hooks_event_budget_seconds", 10)
    monkeypatch.setattr(settings, "hooks_config_path", "")
    hooks.clear()
    yield
    hooks.clear()


def _runtime() -> AgentRuntime:
    return AgentRuntime(gateway=None)


def _task() -> AgentTask:
    return AgentTask(
        id="turn-1", tenant_id="t1", user_id="u1", session_id="s1", content="hi", max_turns=2
    )


def _call(cid: str = "tc-ask") -> dict[str, Any]:
    return {"id": cid, "name": READ_TOOL, "arguments": "{}"}


def _hook_entries(root: Path) -> list[dict[str, Any]]:
    path = root / "hooks_audit.jsonl"
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]


# ── 判定层：`ask` 是一个**独立的档位**，且仍是字符串 ─────────────────────────


async def test_ask_is_reported_as_its_own_kind(monkeypatch: pytest.MonkeyPatch) -> None:
    """`before_tool_use` 返回的仍是字符串（既有断言不受影响），但带 `kind="ask"`。"""
    monkeypatch.setattr(settings, "hooks_enabled", True)
    hooks.register(events.PRE_TOOL_USE, "ask_h", ASK_HOOK)

    decision = await hooks.before_tool_use(task=_task(), tool_call=_call())

    assert decision is not None
    assert decision.kind == DECISION_ASK
    assert "ask_h" in decision, "原因串里要能看出是哪个 hook 要求的"


async def test_deny_kind_unchanged(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "hooks_enabled", True)
    hooks.register(events.PRE_TOOL_USE, "deny_h", DENY_HOOK)

    decision = await hooks.before_tool_use(task=_task(), tool_call=_call())

    assert decision is not None and decision.kind == DECISION_DENY


# ── ① `ask` ⇒ 走审批（工具**没有**被执行）────────────────────────────────────


async def test_ask_routes_to_the_existing_approval_channel(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(settings, "hooks_enabled", True)
    hooks.register(events.PRE_TOOL_USE, "ask_h", ASK_HOOK)
    runtime = _runtime()

    tool_result, evt = await runtime._guarded_execute_tool(_call("tc-ask"), _task())  # noqa: SLF001

    assert tool_result is None, "`ask` 不是拒绝：工具未执行，等用户决定"
    assert evt is not None and evt.type == "approval"
    assert evt.tool_call_id == "tc-ask"
    assert "ops wants a human to confirm" in (evt.content or ""), (
        "确认卡片要说清**谁**在要求确认（运维声明的 hook）"
    )
    assert "tc-ask" in runtime._pending_approvals, "必须注册 pending future，否则 _await_approval 收不到决定"  # noqa: SLF001
    # "问"要留痕（与决定按 tool_call_id 配对）—— 与 confirm 路径同一套审计
    audit_lines = (tmp_path / "approval_audit.jsonl").read_text(encoding="utf-8")
    assert '"asked"' in audit_lines.replace("'", '"'), audit_lines
    # hook 侧的流水里 `ask` 与 `deny` 分开记（配了 ask 却看不出发生过什么是最难查的一类问题）
    last = _hook_entries(tmp_path)[-1]
    assert last["outcome"] == DECISION_ASK
    assert last["blocked"] is False


# ── ② 批准后不再询问 ───────────────────────────────────────────────────────


async def test_approval_does_not_ask_again(monkeypatch: pytest.MonkeyPatch) -> None:
    """`approved=True` 时 `ask` 不再生效 —— 否则会无限循环。"""
    monkeypatch.setattr(settings, "hooks_enabled", True)
    hooks.register(events.PRE_TOOL_USE, "ask_h", ASK_HOOK)
    runtime = _runtime()

    result, ask_reason = await runtime._execute_tool(_call(), _task(), approved=True)  # noqa: SLF001

    assert ask_reason is None, "已批准 ⇒ 不再要求确认"
    assert isinstance(result, dict) and "concurrent batch" not in str(result)


async def test_without_approval_the_same_call_asks(monkeypatch: pytest.MonkeyPatch) -> None:
    """对照：同一调用在 `approved=False` 时确实会被拦下（证明上一条不是"hook 没生效"）。"""
    monkeypatch.setattr(settings, "hooks_enabled", True)
    hooks.register(events.PRE_TOOL_USE, "ask_h", ASK_HOOK)
    runtime = _runtime()

    result, ask_reason = await runtime._execute_tool(_call(), _task())  # noqa: SLF001

    assert result == {} and ask_reason is not None


# ── ③ `deny` 不被"批准过"抵消 ──────────────────────────────────────────────


async def test_deny_still_applies_after_approval(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "hooks_enabled", True)
    hooks.register(events.PRE_TOOL_USE, "deny_h", DENY_HOOK)
    runtime = _runtime()

    result, ask_reason = await runtime._execute_tool(_call(), _task(), approved=True)  # noqa: SLF001

    assert ask_reason is None
    assert "error" in result and "ops forbids this" in str(result["error"]), (
        "hook 只能收紧 —— 批准过一次工具策略不等于批准过运维声明的禁令"
    )


# ── ④ 默认关 ⇒ 零行为变化 ──────────────────────────────────────────────────


async def test_disabled_by_default_ignores_ask(monkeypatch: pytest.MonkeyPatch) -> None:
    hooks.register(events.PRE_TOOL_USE, "ask_h", ASK_HOOK)  # 已注册，但机制默认关
    runtime = _runtime()

    tool_result, evt = await runtime._guarded_execute_tool(_call("tc-off"), _task())  # noqa: SLF001

    assert evt is None, "默认关时不该产生任何审批"
    assert tool_result is not None, "默认关时工具照常执行"
    assert "tc-off" not in runtime._pending_approvals  # noqa: SLF001


# ── ⑤ 并发批：不能交互 ⇒ 显式错误 ──────────────────────────────────────────


async def test_concurrent_batch_refuses_ask_explicitly(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "hooks_enabled", True)
    hooks.register(events.PRE_TOOL_USE, "ask_h", ASK_HOOK)
    runtime = _runtime()

    tool_result, evt = await runtime._guarded_execute_tool(  # noqa: SLF001
        _call("tc-par"), _task(), allow_approval=False
    )

    assert evt is None, "并发批不产生事件（组内 gather 的前提）"
    assert tool_result is not None and "error" in tool_result
    assert "concurrent batch" in str(tool_result["error"]), "必须**显式**说明为什么没执行"
    assert "tc-par" not in runtime._pending_approvals, "不得留下无人认领的 pending future"  # noqa: SLF001
