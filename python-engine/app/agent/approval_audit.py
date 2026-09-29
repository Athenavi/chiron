"""审批决策审计（C5 · `edit` 语义）—— JSONL 追加式流水。

与 `app/tools/exec_audit.py` **同构**（JSONL + `logs/` + 失败不阻断），但记录对象不同：
exec_audit 记「命令真的被执行了」（含退出码 / 耗时），这里记**审批这一动作本身** ——
谁、在什么时候、把哪个调用批成了什么。

**为什么必须单独留痕**：`edit` 让「用户批准的对象」与「模型原本请求的对象」**可以不同**
（用户改的是"将要执行的那一次调用"）。只记其一都无法回答事后最要紧的那个问题 ——
"用户到底批准了什么"（方案 01 §3.5）。因此原始参数与编辑后参数**两份都落**，
并带上动作级别的前后对比（级别升高会被 fail-closed 拒绝，那条拒绝同样要有痕迹）。

参数文本必须**脱敏 + 截断**：它可能夹带模型从上下文里读到的密钥
（规则集见 `app/subagent/redact.py`）。
"""

from __future__ import annotations

import json
import logging
import time
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

#: 落盘开关。默认**开**：审计属安全能力而非可选项；落盘失败只告警。
APPROVAL_AUDIT_ENABLED = True

#: 单份参数文本的最大字符数（脱敏之后再截断）
ARGS_MAX_CHARS = 2000

#: 审计文件名（与 `exec_audit.jsonl` / `plugin_audit.jsonl` 并列，便于统一收集）
APPROVAL_AUDIT_FILENAME = "approval_audit.jsonl"

#: 落盘目录覆盖（None = 用 CWD 下 `logs/`）。测试 monkeypatch 它指向 tmp_path。
AUDIT_DIR: Path | None = None


def _audit_dir() -> Path:
    if AUDIT_DIR is not None:
        return AUDIT_DIR
    from app.config import settings

    return Path(getattr(settings, "log_dir", "") or "logs")


def _summarize_args(args: dict[str, Any] | None) -> str:
    """把参数对象序列化后脱敏 + 截断（供审计落盘）。"""
    if not args:
        return ""
    from app.subagent.redact import redact_text

    try:
        text = json.dumps(args, ensure_ascii=False, sort_keys=True, default=str)
    except (TypeError, ValueError):
        text = str(args)
    redacted, _hits = redact_text(text)
    if len(redacted) > ARGS_MAX_CHARS:
        dropped = len(redacted) - ARGS_MAX_CHARS
        return f"{redacted[:ARGS_MAX_CHARS]}...(truncated {dropped} chars)"
    return redacted


def record_approval_decision(
    *,
    tool_call_id: str,
    tool: str,
    decision: str,
    reason: str = "",
    level_before: str = "",
    level_after: str = "",
    original_arguments: dict[str, Any] | None = None,
    edited_arguments: dict[str, Any] | None = None,
) -> None:
    """记录一次审批决策（同步写，失败不阻断调用方）。

    身份从 tool context（contextvars）取 —— 与执行审计同源，调用方不必透传，
    也就不会出现"某条路径忘了带身份"的漏记。

    Args:
        tool_call_id: 被审批的调用 id。
        tool: 工具名。
        decision: 最终生效的决策（`approve` / `reject` / `edit`；级别升高被拒时也记 `edit`）。
        reason: 决策原因 / 拒绝理由（用户备注或系统给出的升级说明）。
        level_before: 原始参数下的动作级别。
        level_after: 编辑后参数下的动作级别（未 edit 时为空）。
        original_arguments: 模型原本请求的参数。
        edited_arguments: 用户编辑后的参数（未 edit 时为 None）。
    """
    if not APPROVAL_AUDIT_ENABLED:
        return
    try:
        from app.tools.context import get_session_id, get_tenant_id, get_user_id

        log_dir = _audit_dir()
        log_dir.mkdir(parents=True, exist_ok=True)
        entry: dict[str, Any] = {
            "ts": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "tool_call_id": tool_call_id,
            "tool": tool,
            "tenant": get_tenant_id() or "",
            "user": get_user_id() or "",
            "session": get_session_id() or "",
            "decision": decision,
            "level_before": level_before,
            "level_after": level_after,
        }
        if reason:
            entry["reason"] = reason
        if original_arguments is not None:
            entry["original_arguments"] = _summarize_args(original_arguments)
        if edited_arguments is not None:
            entry["edited_arguments"] = _summarize_args(edited_arguments)
        with (log_dir / APPROVAL_AUDIT_FILENAME).open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except Exception:  # noqa: BLE001 — 审计失败不影响审批结果
        logger.warning("approval audit log write failed", exc_info=True)
