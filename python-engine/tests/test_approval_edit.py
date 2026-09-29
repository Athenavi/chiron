"""C5：审批 `edit` 语义（方案 01 §3.5）。

对齐方案§3.5 的四条验收：
① `edit` 后执行的是**编辑后的参数**；
② `edit` 成**更高危**动作时**不直接执行**（防"用 edit 绕过分级"）；
③ 审计里**同时**可见原始与编辑后参数；
④ 旧版前端（只发 `approved: bool`）行为不变。

另覆盖协议解析（`parse_approval_payload`）与跨副本决策载荷的**新旧格式兼容**
（`ApprovalDecision.from_raw`）—— 它们是"决策能如实送达"的前提。
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from app.agent.runtime import (
    DECISION_APPROVE,
    DECISION_EDIT,
    DECISION_REJECT,
    AgentRuntime,
    ApprovalDecision,
    ApprovalTicket,
    parse_approval_payload,
)
from app.agent.tool_policy import args_hash, tool_level

# ── 协议解析（`/v1/agent/approval` 的两个端点共用） ──────────────────────


def test_legacy_approved_true_maps_to_approve():
    """④ 旧形态：只发 `approved: bool`。"""
    decision = parse_approval_payload(approved=True)
    assert isinstance(decision, ApprovalDecision)
    assert decision.decision == DECISION_APPROVE
    assert decision.approved is True


def test_legacy_approved_false_maps_to_reject():
    decision = parse_approval_payload(approved=False)
    assert isinstance(decision, ApprovalDecision)
    assert decision.decision == DECISION_REJECT
    assert decision.approved is False


def test_missing_both_is_an_error_not_a_default_approval():
    """两者都缺 → 报错。**默认批准**会让"漏发字段"变成放行。"""
    result = parse_approval_payload(approved=None)
    assert isinstance(result, str)


def test_new_decision_field():
    assert parse_approval_payload(decision="approve").decision == DECISION_APPROVE  # type: ignore[union-attr]
    assert parse_approval_payload(decision="REJECT").decision == DECISION_REJECT  # type: ignore[union-attr]


def test_unknown_decision_is_rejected():
    result = parse_approval_payload(decision="maybe")
    assert isinstance(result, str) and "unknown decision" in result


def test_edit_requires_json_object_arguments():
    ok = parse_approval_payload(decision="edit", arguments='{"cmd": "ls -la"}')
    assert isinstance(ok, ApprovalDecision)
    assert ok.arguments == {"cmd": "ls -la"}

    for bad in ("", "not json", "[1,2]", '"text"'):
        result = parse_approval_payload(decision="edit", arguments=bad)
        assert isinstance(result, str), f"{bad!r} 应被拒绝"


def test_edit_accepts_already_parsed_dict():
    """端点可能直接传 dict（main.py 的 body 就是 JSON 解出来的）。"""
    ok = parse_approval_payload(decision="edit", arguments={"cmd": "ls"})
    assert isinstance(ok, ApprovalDecision) and ok.arguments == {"cmd": "ls"}


# ── 跨副本决策载荷：新旧格式兼容 ───────────────────────────────────────


def test_decision_payload_roundtrip():
    original = ApprovalDecision(decision=DECISION_EDIT, arguments={"a": 1}, reason="改窄一点")
    parsed = ApprovalDecision.from_raw(original.to_json())
    assert parsed is not None
    assert parsed.decision == DECISION_EDIT
    assert parsed.arguments == {"a": 1}
    assert parsed.reason == "改窄一点"


@pytest.mark.parametrize(
    ("raw", "expected"),
    [("1", DECISION_APPROVE), (b"0", DECISION_REJECT), ("true", DECISION_APPROVE), ("false", DECISION_REJECT)],
)
def test_legacy_redis_values_still_parse(raw: Any, expected: str):
    """升级期间 Redis 里仍有旧的 `"1"`/`"0"` 决策键 —— 必须能读出来，否则审批被吞掉。"""
    parsed = ApprovalDecision.from_raw(raw)
    assert parsed is not None and parsed.decision == expected


def test_edit_counts_as_approved():
    assert ApprovalDecision(decision=DECISION_EDIT, arguments={}).approved is True


# ── `_await_approval` 的执行语义 ─────────────────────────────────────────


class _FakeRedis:
    """只实现票据读写用到的三个命令。"""

    def __init__(self) -> None:
        self.store: dict[str, Any] = {}

    async def set(self, key: Any, value: Any, ex: int | None = None) -> bool:
        self.store[str(key)] = value
        return True

    async def get(self, key: Any) -> Any:
        return self.store.get(str(key))

    async def delete(self, *keys: Any) -> int:
        return sum(1 for k in keys if self.store.pop(str(k), None) is not None)


@pytest.fixture
def fake_redis(monkeypatch: pytest.MonkeyPatch) -> _FakeRedis:
    import app.redis_client as redis_client

    redis = _FakeRedis()

    async def _get_redis() -> _FakeRedis:
        return redis

    monkeypatch.setattr(redis_client, "get_redis", _get_redis)
    return redis


@pytest.fixture(autouse=True)
def _audit_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """审计落到 tmp，避免污染仓库 logs/；同时便于断言其内容。"""
    from app.agent import approval_audit

    monkeypatch.setattr(approval_audit, "AUDIT_DIR", tmp_path)
    return tmp_path


def _task() -> SimpleNamespace:
    return SimpleNamespace(id="turn_1")


def _call(arguments: dict[str, Any], name: str = "shell_exec") -> dict[str, Any]:
    return {"id": "call_1", "name": name, "arguments": json.dumps(arguments)}


def _runtime() -> AgentRuntime:
    """最小 runtime：`_await_approval` 只用到 `_pending_approvals` 与若干方法。"""
    runtime = AgentRuntime.__new__(AgentRuntime)
    runtime._pending_approvals = {}
    return runtime


async def _prepare(
    runtime: AgentRuntime, call: dict[str, Any], decision: ApprovalDecision, redis: _FakeRedis
) -> None:
    """模拟"审批已发起"（票据在 Redis、future 待唤醒），再投递决策。"""
    import asyncio

    tc_id = call["id"]
    runtime._pending_approvals[tc_id] = asyncio.get_running_loop().create_future()
    await runtime._store_approval_ticket(
        ApprovalTicket(
            tool_call_id=tc_id,
            tool_name=call["name"],
            args_hash=args_hash(call["name"], json.loads(call["arguments"])),
            turn_id="turn_1",
        )
    )
    runtime._pending_approvals[tc_id].set_result(decision)


def _capture_execution(monkeypatch: pytest.MonkeyPatch, calls: list[dict[str, Any]]) -> None:
    """替换 `_execute_tool` 并记录它收到的 tool_call。

    注意：类属性经实例访问会绑定 `self`，所以替身**必须**带 `self` 参数
    （否则 TypeError 会被上层吞成"工具执行失败"，表现为"看起来执行了但结果全是错"）。
    """

    async def _fake(self: AgentRuntime, tool_call: dict[Any, Any], task: Any) -> dict[Any, Any]:
        calls.append(json.loads(tool_call["arguments"]))
        return {"output": "ok"}

    monkeypatch.setattr(AgentRuntime, "_execute_tool", _fake)


def _audit_entries(root: Path) -> list[dict[str, Any]]:
    path = root / "approval_audit.jsonl"
    if not path.exists():
        return []
    return [
        json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line
    ]


async def test_edit_executes_the_edited_arguments(
    monkeypatch: pytest.MonkeyPatch, fake_redis: _FakeRedis, _audit_dir: Path
) -> None:
    """① 批准的是**编辑后的那一次调用**（整份替换，不是补丁）。"""
    executed: list[dict[str, Any]] = []
    _capture_execution(monkeypatch, executed)

    runtime = _runtime()
    call = _call({"cmd": "ls"})
    await _prepare(
        runtime, call, ApprovalDecision(decision=DECISION_EDIT, arguments={"cmd": "ls -la"}), fake_redis
    )

    result = await runtime._await_approval(call, _task(), timeout=1)
    assert result.get("error") is None
    assert executed == [{"cmd": "ls -la"}], "执行的必须是编辑后的参数"


async def test_plain_approve_still_executes_original_arguments(
    monkeypatch: pytest.MonkeyPatch, fake_redis: _FakeRedis, _audit_dir: Path
) -> None:
    """④ 旧语义不变：只批准时执行原始参数。"""
    executed: list[dict[str, Any]] = []
    _capture_execution(monkeypatch, executed)

    runtime = _runtime()
    call = _call({"cmd": "ls"})
    await _prepare(runtime, call, ApprovalDecision(decision=DECISION_APPROVE), fake_redis)

    result = await runtime._await_approval(call, _task(), timeout=1)
    assert result.get("error") is None
    assert executed == [{"cmd": "ls"}]


async def test_reject_does_not_execute(
    monkeypatch: pytest.MonkeyPatch, fake_redis: _FakeRedis, _audit_dir: Path
) -> None:
    executed: list[dict[str, Any]] = []
    _capture_execution(monkeypatch, executed)

    runtime = _runtime()
    call = _call({"cmd": "ls"})
    await _prepare(runtime, call, ApprovalDecision(decision=DECISION_REJECT), fake_redis)

    result = await runtime._await_approval(call, _task(), timeout=1)
    assert "denied" in result.get("error", "")
    assert executed == []


async def test_edit_escalation_is_refused(
    monkeypatch: pytest.MonkeyPatch, fake_redis: _FakeRedis, _audit_dir: Path
) -> None:
    """② 编辑把动作升级（write → delete）⇒ **不直接执行**。"""
    # 前置事实：这一对参数确实构成"级别升高"（否则本用例测试的就不是它了）
    assert tool_level("shell_exec", {"cmd": "ls"}) == "write"
    assert tool_level("shell_exec", {"cmd": "rm -rf /"}) == "delete"

    executed: list[dict[str, Any]] = []
    _capture_execution(monkeypatch, executed)

    runtime = _runtime()
    call = _call({"cmd": "ls"})
    await _prepare(
        runtime,
        call,
        ApprovalDecision(decision=DECISION_EDIT, arguments={"cmd": "rm -rf /"}),
        fake_redis,
    )

    result = await runtime._await_approval(call, _task(), timeout=1)
    assert executed == [], "升级后的动作绝不能被执行"
    error = result.get("error", "")
    assert "escalate" in error and "write" in error and "delete" in error


async def test_audit_records_both_argument_sets(
    monkeypatch: pytest.MonkeyPatch, fake_redis: _FakeRedis, _audit_dir: Path
) -> None:
    """③ 审计里同时可见原始与编辑后参数（否则无法回答"用户批准了什么"）。"""
    executed: list[dict[str, Any]] = []
    _capture_execution(monkeypatch, executed)

    runtime = _runtime()
    call = _call({"cmd": "ls"})
    await _prepare(
        runtime,
        call,
        ApprovalDecision(decision=DECISION_EDIT, arguments={"cmd": "ls -la"}, reason="加个 -la"),
        fake_redis,
    )
    await runtime._await_approval(call, _task(), timeout=1)

    entries = _audit_entries(_audit_dir)
    assert entries, "审批决策必须留审计"
    last = entries[-1]
    assert last["decision"] == DECISION_EDIT
    assert "ls" in last["original_arguments"]
    assert "ls -la" in last["edited_arguments"]
    assert last["level_before"] == "write"
    assert last["level_after"] == "write"
    assert last["reason"] == "加个 -la"


async def test_edit_escalation_is_audited(
    monkeypatch: pytest.MonkeyPatch, fake_redis: _FakeRedis, _audit_dir: Path
) -> None:
    """被拦下的那次也要留痕（否则"为什么没执行"无从查证）。"""
    _capture_execution(monkeypatch, [])

    runtime = _runtime()
    call = _call({"cmd": "ls"})
    await _prepare(
        runtime,
        call,
        ApprovalDecision(decision=DECISION_EDIT, arguments={"cmd": "rm -rf /"}),
        fake_redis,
    )
    await runtime._await_approval(call, _task(), timeout=1)

    last = _audit_entries(_audit_dir)[-1]
    assert last["level_before"] == "write"
    assert last["level_after"] == "delete"
    assert "escalate" in last["reason"]


async def test_edit_ticket_is_rewritten_to_edited_arguments(
    monkeypatch: pytest.MonkeyPatch, fake_redis: _FakeRedis, _audit_dir: Path
) -> None:
    """不变量：票据在 edit 后指向**被批准并执行**的那份参数。"""
    _capture_execution(monkeypatch, [])

    runtime = _runtime()
    call = _call({"cmd": "ls"})
    await _prepare(
        runtime, call, ApprovalDecision(decision=DECISION_EDIT, arguments={"cmd": "ls -la"}), fake_redis
    )
    await runtime._await_approval(call, _task(), timeout=1)

    from app.redis_keys import rkey

    raw = fake_redis.store.get(rkey("approval_req:call_1"))
    assert raw is not None, "edit 后票据应被重写"
    ticket = ApprovalTicket.from_json(raw)
    assert ticket is not None
    assert ticket.args_hash == args_hash("shell_exec", {"cmd": "ls -la"})
