"""S5-3 回归：执行类工具的审计落盘。

三件事必须成立：
1. 每次执行留一条 JSONL，且带租户/用户/会话身份；
2. **被拦下**与"执行成功/失败"能区分（`outcome` 字段）；
3. 命令文本**脱敏**后再落盘（模型可能把上下文里的密钥带进命令）。
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.tools.context import set_tool_context


@pytest.fixture
def audit_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """把审计落盘目录指到临时目录，避免污染仓库 logs/。

    `Settings` 是 pydantic 模型（拒绝设置未声明字段），所以覆盖的是 exec_audit 的
    模块级 `AUDIT_DIR`，而不是 `settings.log_dir`。
    """
    from app.tools import exec_audit

    monkeypatch.setattr(exec_audit, "AUDIT_DIR", tmp_path)
    return tmp_path


def _entries(root: Path) -> list[dict]:
    path = root / "exec_audit.jsonl"
    if not path.exists():
        return []
    lines = path.read_text(encoding="utf-8").strip().splitlines()
    return [json.loads(line) for line in lines if line]


def test_records_identity_and_outcome(audit_dir: Path):
    from app.tools.exec_audit import record_execution

    set_tool_context(session_id="s-audit", user_id="u-audit", tenant_id="t-audit")
    record_execution(tool="shell_exec", command="echo hi", outcome="ok", exit_code=0)

    entries = _entries(audit_dir)
    assert len(entries) == 1
    entry = entries[0]
    assert entry["tool"] == "shell_exec"
    assert entry["tenant"] == "t-audit"
    assert entry["user"] == "u-audit"
    assert entry["session"] == "s-audit"
    assert entry["outcome"] == "ok"
    assert entry["exit_code"] == 0


def test_command_is_redacted_before_persisting(audit_dir: Path):
    from app.tools.exec_audit import record_execution

    set_tool_context(session_id="s-redact")
    record_execution(
        tool="shell_exec", command="export password=hunter2secret", outcome="ok"
    )

    entry = _entries(audit_dir)[0]
    assert "hunter2secret" not in entry["command"], "密钥不得以明文落盘"
    assert "[REDACTED" in entry["command"]


def test_long_command_is_truncated(audit_dir: Path):
    from app.tools.exec_audit import COMMAND_MAX_CHARS, record_execution

    set_tool_context(session_id="s-trunc")
    record_execution(tool="run_code", command="x" * (COMMAND_MAX_CHARS * 2), outcome="ok")

    entry = _entries(audit_dir)[0]
    assert len(entry["command"]) < COMMAND_MAX_CHARS * 2
    assert "truncated" in entry["command"]


@pytest.mark.asyncio
async def test_blocked_command_is_audited(audit_dir: Path):
    """端到端：被准入检查拦下的命令同样要留痕（审计的主要目的之一）。"""
    from app.tools.terminal import _terminal, persistent_shell

    set_tool_context(session_id="s-blocked")
    out = await persistent_shell("rm -rf workspace")
    assert out["exit_code"] == -1
    await _terminal.close_all()

    entries = [e for e in _entries(audit_dir) if e["tool"] == "persistent_shell"]
    assert entries, "被拦下的命令未落审计"
    assert entries[-1]["outcome"] == "blocked"
    assert entries[-1]["reason"]
