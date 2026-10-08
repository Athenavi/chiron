"""`app/plugins/audit.py`：插件流水必须是**可复用**的，且与另外三条流水同口径。

背景（2026-10-08）：plugin audit 此前是 `app/api/plugins.py` 里的**内联写盘**，于是运行时拉起
插件进程的两处（`mcp/client.py`、`skill/manager.py`）无法记录 —— 抽成模块后才接上
（覆盖度由 `tests/test_exec_audit_inventory.py` 机械保证）。

这里钉住三件事：① `ts` 是 **UTC + 毫秒**（四条流水会被同一套流程合并读取，本地时间/秒级会
让跨实例排序错位）；② 身份（tenant/user/session）随上下文自动带上；③ 只记 `input_keys`，
**不记输入值**（值里可能有密钥）。
"""

import json
import re

import pytest

from app.plugins import audit as plugin_audit
from app.tools.context import set_tool_context

#: `YYYY-MM-DDTHH:MM:SS.sssZ` —— 与 app/audit_log.py 的 utc_timestamp 同款
_UTC_MS = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}Z$")


@pytest.fixture
def audit_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(plugin_audit, "AUDIT_DIR", tmp_path)
    return tmp_path


def _entries(directory) -> list[dict]:
    path = directory / plugin_audit.PLUGIN_AUDIT_FILENAME
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def test_record_plugin_writes_utc_ts_and_identity(audit_dir):
    set_tool_context(tenant_id="t1", user_id="u1", session_id="s1")
    plugin_audit.record_plugin(plugin="git-helper", action="spawn", success=True, input_keys=["a", "b"])

    entry = _entries(audit_dir)[-1]
    assert entry["plugin"] == "git-helper"
    assert entry["action"] == "spawn"
    assert (entry["tenant"], entry["user"], entry["session"]) == ("t1", "u1", "s1")
    assert entry["success"] is True
    assert entry["input_keys"] == ["a", "b"]
    assert _UTC_MS.match(entry["ts"]), f"ts 必须是 UTC+毫秒（与另外三条流水一致），得到 {entry['ts']!r}"


def test_record_plugin_records_failure_reason(audit_dir):
    plugin_audit.record_plugin(
        plugin="web", action="spawn", success=False, error="command not found"
    )
    entry = _entries(audit_dir)[-1]
    assert entry["success"] is False
    assert entry["error"] == "command not found"


def test_record_plugin_never_raises(tmp_path, monkeypatch):
    """审计失败不能影响插件执行：目录不可写时只告警。"""
    monkeypatch.setattr(plugin_audit, "AUDIT_DIR", tmp_path / "nope" / "deep")
    monkeypatch.setattr(plugin_audit.Path, "mkdir", _boom)
    plugin_audit.record_plugin(plugin="x", action="spawn")  # 不应抛异常


def _boom(*_args, **_kwargs):
    raise PermissionError("read-only dir")
