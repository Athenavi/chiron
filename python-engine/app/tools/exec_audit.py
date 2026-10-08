"""exec_audit — 执行类工具的审计落盘（S5-3）。

与 `app/api/plugins.py` 的 plugin audit **同构**（JSONL + `logs/` + 失败不阻断），
但记录对象是「agent 执行的命令」：谁（租户/用户/会话）在哪个工具里执行了什么、
结果如何、是否被准入检查拦下。

**为什么需要它**：执行类工具（`shell_exec` / `run_code` / `persistent_shell`）是唯一
能让模型触达宿主能力面的一组工具；此前的痕迹只散落在 trace span 里且**不含命令文本**，
出事时无法回答"谁在哪个租户下执行过什么"。

**命令文本必须脱敏 + 截断**：它可能夹带模型从上下文里读到的密钥（规则集见
`app/subagent/redact.py`）。
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

from app.audit_log import utc_timestamp

logger = logging.getLogger(__name__)

#: 落盘开关。默认**开**：审计属安全能力而非可选项；落盘失败只告警（不影响执行）。
EXEC_AUDIT_ENABLED = True

#: 命令摘要的最大字符数（脱敏之后再截断）
COMMAND_MAX_CHARS = 500

#: 审计文件名（与 `plugin_audit.jsonl` 并列，便于统一收集）
EXEC_AUDIT_FILENAME = "exec_audit.jsonl"

#: 落盘目录覆盖（None = 用 CWD 下 `logs/`，与 plugin audit 同处）。
#: 测试通过 monkeypatch 此变量把落盘指向 tmp_path，避免污染仓库。
AUDIT_DIR: Path | None = None

#: 结果分类
OUTCOME_OK = "ok"
OUTCOME_BLOCKED = "blocked"
OUTCOME_TIMEOUT = "timeout"
OUTCOME_ERROR = "error"


def _audit_dir() -> Path:
    """审计落盘目录。

    `Settings` 上没有 `log_dir` 字段（与 plugin audit 同样的兜底），因此默认固定为
    CWD 下 `logs/` —— 与引擎的 logs/ 保持一致。
    """
    if AUDIT_DIR is not None:
        return AUDIT_DIR
    from app.config import settings

    return Path(getattr(settings, "log_dir", "") or "logs")


def _summarize_command(command: str) -> str:
    """脱敏 + 截断命令文本（供审计落盘）。"""
    from app.subagent.redact import redact_text

    redacted, _hits = redact_text(command or "")
    if len(redacted) > COMMAND_MAX_CHARS:
        dropped = len(redacted) - COMMAND_MAX_CHARS
        return f"{redacted[:COMMAND_MAX_CHARS]}...(truncated {dropped} chars)"
    return redacted


def record_execution(
    *,
    tool: str,
    command: str,
    outcome: str,
    exit_code: int | None = None,
    reason: str | None = None,
    duration_ms: int | None = None,
) -> None:
    """记录一次执行（同步写，失败不阻断调用方）。

    身份从 tool context（contextvars）取 —— 与工具链其它地方同源，调用方不必透传，
    也就不会出现"某条调用路径忘了带身份"的漏记。

    Args:
        tool: 工具名（`shell_exec` / `run_code` / `persistent_shell`）。
        command: 命令或代码文本（内部会脱敏 + 截断）。
        outcome: `ok` / `blocked` / `timeout` / `error`。
        exit_code: 进程退出码（被拦下或无法判定时为 None）。
        reason: 被拦下的原因（`outcome=blocked` 时给出）。
        duration_ms: 耗时（毫秒）。
    """
    if not EXEC_AUDIT_ENABLED:
        return
    try:
        from app.tools.context import get_session_id, get_tenant_id, get_user_id

        log_dir = _audit_dir()
        log_dir.mkdir(parents=True, exist_ok=True)
        entry: dict[str, Any] = {
            "ts": utc_timestamp(),
            "tool": tool,
            "tenant": get_tenant_id() or "",
            "user": get_user_id() or "",
            "session": get_session_id() or "",
            "command": _summarize_command(command),
            "outcome": outcome,
            "exit_code": exit_code,
        }
        if reason:
            entry["reason"] = reason
        if duration_ms is not None:
            entry["duration_ms"] = duration_ms
        with (log_dir / EXEC_AUDIT_FILENAME).open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except Exception:  # noqa: BLE001 — 审计失败不影响执行结果
        logger.warning("exec audit log write failed", exc_info=True)
