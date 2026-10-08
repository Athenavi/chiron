"""审批审计（`approval_audit.jsonl`）的行为验证。

`approval_audit.py` 此前**零用例**，而它是 `edit` 语义唯一的留痕处：用户批准的对象
与模型原本请求的对象**可以不同**，所以"原始参数"与"编辑后参数"两份都必须落盘，
否则事后无法回答"用户到底批准了什么"（见模块 docstring）。

顺带钉住三件容易悄悄退化的事：
1. 身份（租户/用户/会话）从工具上下文取 —— 这是 DSH 这类本地单用户 harness 没有的维度；
2. 参数**脱敏**：审计路径先把参数 `json.dumps`，键名因此带引号（见 tests/test_redact.py 的回归）；
3. 时间戳是 **UTC + 毫秒**（三份审计流水要能被同一套收集流程合并排序）。
"""
from __future__ import annotations

import json
import re
from datetime import UTC, datetime
from pathlib import Path

import pytest

from app.tools.context import set_tool_context

FIXED_TS = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}Z$")


@pytest.fixture
def audit_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    from app.agent import approval_audit

    monkeypatch.setattr(approval_audit, "AUDIT_DIR", tmp_path)
    return tmp_path


@pytest.fixture(autouse=True)
def _clean_tool_context():
    yield
    from app.tools import context as tool_context

    tool_context._current_context.set(None)


def _entries(root: Path) -> list[dict]:
    path = root / "approval_audit.jsonl"
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]


def test_records_identity_and_decision(audit_dir: Path):
    from app.agent.approval_audit import record_approval_decision

    set_tool_context(session_id="s-appr", user_id="u-appr", tenant_id="t-appr")
    record_approval_decision(
        tool_call_id="call-1",
        tool="shell_exec",
        decision="approve",
        level_before="write",
        level_after="",
    )

    entry = _entries(audit_dir)[0]
    assert entry["tool_call_id"] == "call-1"
    assert entry["tool"] == "shell_exec"
    assert entry["decision"] == "approve"
    assert entry["tenant"] == "t-appr"
    assert entry["user"] == "u-appr"
    assert entry["session"] == "s-appr"
    assert entry["level_before"] == "write"


def test_records_both_original_and_edited_arguments(audit_dir: Path):
    """`edit` 的核心承诺：**两份**参数都在，否则无法回答"用户批准了什么"。"""
    from app.agent.approval_audit import record_approval_decision

    set_tool_context(session_id="s-edit")
    record_approval_decision(
        tool_call_id="call-edit",
        tool="shell_exec",
        decision="edit",
        original_arguments={"command": "ls -la"},
        edited_arguments={"command": "ls -la /tmp"},
        level_before="write",
        level_after="write",
    )

    entry = _entries(audit_dir)[0]
    assert "ls -la" in entry["original_arguments"]
    assert "/tmp" in entry["edited_arguments"]


def test_arguments_are_redacted_before_persisting(audit_dir: Path):
    """参数先 `json.dumps` 再脱敏：JSON 键名带引号也必须命中（本轮修掉的缺陷）。"""
    from app.agent.approval_audit import record_approval_decision

    set_tool_context(session_id="s-redact")
    record_approval_decision(
        tool_call_id="call-secret",
        tool="web_fetch",
        decision="approve",
        original_arguments={"password": "hunter2secret", "url": "https://example.com"},
    )

    entry = _entries(audit_dir)[0]
    assert "hunter2secret" not in entry["original_arguments"], "密钥不得以明文落盘"
    assert "[REDACTED" in entry["original_arguments"]


def test_long_arguments_are_truncated(audit_dir: Path):
    from app.agent.approval_audit import ARGS_MAX_CHARS, record_approval_decision

    set_tool_context(session_id="s-trunc")
    record_approval_decision(
        tool_call_id="call-long",
        tool="write_file",
        decision="approve",
        original_arguments={"content": "x" * (ARGS_MAX_CHARS * 2)},
    )

    entry = _entries(audit_dir)[0]
    assert len(entry["original_arguments"]) < ARGS_MAX_CHARS * 2
    assert "truncated" in entry["original_arguments"]


def test_timestamp_is_utc_with_milliseconds(audit_dir: Path):
    from app.agent.approval_audit import record_approval_decision

    set_tool_context(session_id="s-ts")
    record_approval_decision(tool_call_id="call-ts", tool="read_file", decision="approve")

    ts = _entries(audit_dir)[0]["ts"]
    assert FIXED_TS.match(ts), ts
    parsed = datetime.strptime(ts, "%Y-%m-%dT%H:%M:%S.%fZ").replace(tzinfo=UTC)
    assert abs((datetime.now(UTC) - parsed).total_seconds()) < 5, "ts 必须是 UTC（否则差一个时区偏移）"


def test_disabled_writes_nothing(audit_dir: Path, monkeypatch: pytest.MonkeyPatch):
    from app.agent import approval_audit

    monkeypatch.setattr(approval_audit, "APPROVAL_AUDIT_ENABLED", False)
    approval_audit.record_approval_decision(tool_call_id="c", tool="t", decision="approve")

    assert _entries(audit_dir) == []


def test_write_failure_does_not_raise(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """审计是旁路：落盘失败只告警，不能让审批流程失败。"""
    from app.agent import approval_audit

    blocker = tmp_path / "not-a-dir"
    blocker.write_text("x", encoding="utf-8")  # mkdir 必然失败
    monkeypatch.setattr(approval_audit, "AUDIT_DIR", blocker)

    approval_audit.record_approval_decision(tool_call_id="c", tool="t", decision="approve")


def test_all_audit_modules_share_one_timestamp_helper():
    """三份流水的 ts 必须来自同一处 —— 否则又会各写一份（见 vendor/规划.md §4）。"""
    from app.agent import approval_audit
    from app.audit_log import utc_timestamp
    from app.hooks import audit as hooks_audit
    from app.tools import exec_audit

    for module in (approval_audit, hooks_audit, exec_audit):
        assert module.utc_timestamp is utc_timestamp, f"{module.__name__} 没有用共享时间戳"
