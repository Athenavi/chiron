"""`tool_job`（后台命令）必须进 `exec_audit` —— 并且要带上"**谁**"。

背景（2026-10-08 实测）：`shell_exec` / `run_code` / `persistent_shell` 三条前台执行路径都写
`exec_audit.jsonl`，而**后台命令** `tool_job`（同一个沙箱、同一套逃逸拦截）**完全没有痕迹** ——
偏偏它是最难事后观察的一条：跑完就结束，事后只剩 Redis 里的结果，答不出"谁在哪个租户下执行过什么"。

另有一个连带缺陷：队列 worker 在**另一个任务**里执行，`contextvars` 不会跟过去，投递载荷里也
没带身份 ⇒ 即便补上审计，也只能记下命令、记不下人。因此身份随载荷投递、在 worker 里恢复。
"""

import json

import pytest

from app.tools import exec_audit
from app.tools.context import get_session_id, get_tenant_id, get_user_id, set_tool_context
from app.tools.job_runner import execute_tool_job, restore_job_context


@pytest.fixture
def audit_dir(tmp_path, monkeypatch):
    """把审计落盘指向 tmp_path（默认是 CWD/logs，测试不能污染仓库）。"""
    monkeypatch.setattr(exec_audit, "AUDIT_DIR", tmp_path)
    return tmp_path


def _entries(directory) -> list[dict]:
    path = directory / exec_audit.EXEC_AUDIT_FILENAME
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


@pytest.mark.asyncio
async def test_blocked_job_is_audited(audit_dir):
    """被逃逸拦截的后台命令也要留痕（拒绝本身是安全事件）。"""
    result = await execute_tool_job(None, "job-blocked", "rm -rf /")
    assert result["status"] == "blocked"

    entries = _entries(audit_dir)
    assert entries, "被拦截的后台命令必须写审计"
    last = entries[-1]
    assert last["tool"] == "tool_job"
    assert last["outcome"] == exec_audit.OUTCOME_BLOCKED
    assert "blocked" in (last.get("reason") or "")


@pytest.mark.asyncio
async def test_completed_job_is_audited_with_identity(audit_dir):
    """成功的后台命令：记下命令、结果分类，以及**发起人**（租户/用户/会话）。"""
    set_tool_context(tenant_id="t1", user_id="u1", session_id="s1")
    result = await execute_tool_job(None, "job-ok", "echo hi")
    assert result["status"] == "completed"

    last = _entries(audit_dir)[-1]
    assert last["outcome"] == exec_audit.OUTCOME_OK
    assert (last["tenant"], last["user"], last["session"]) == ("t1", "u1", "s1")


def test_restore_job_context_puts_identity_back():
    """队列 worker 侧的恢复：载荷里带什么，上下文中就恢复什么。"""
    set_tool_context(tenant_id="", user_id="", session_id="")
    restore_job_context(
        {"job_id": "j1", "tenant_id": "t9", "user_id": "u9", "session_id": "s9"}
    )
    assert (get_tenant_id(), get_user_id(), get_session_id()) == ("t9", "u9", "s9")


def test_restore_job_context_ignores_missing_identity():
    """载荷没有身份（旧任务）时不得清空已有上下文，也不得抛错。"""
    set_tool_context(tenant_id="t-keep", user_id="u-keep", session_id="s-keep")
    restore_job_context({"job_id": "j2", "command": "echo hi"})
    assert (get_tenant_id(), get_user_id(), get_session_id()) == ("t-keep", "u-keep", "s-keep")
