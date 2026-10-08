"""批 G hook 执行的审计落盘（方案 03 §3.2）。

每次执行记一条 JSONL：**谁、哪个租户、哪个事件、耗时、退出码、是否阻断**。
与 `app/tools/exec_audit.py`、plugin audit **同构**（JSONL + `logs/` + 失败不阻断）。

为什么必须留痕：hook 是引擎旁执行的第三方代码（用户自定义时更是租户代码），
出事时要能回答"谁在哪个租户下、经由哪个 hook、在哪个事件上做了什么、有没有拦下工具"。
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

from app.audit_log import utc_timestamp

logger = logging.getLogger(__name__)

#: 落盘开关。默认**开** —— 审计属安全能力而非可选项。
HOOK_AUDIT_ENABLED = True

#: 审计文件名（与 `exec_audit.jsonl` / `plugin_audit.jsonl` 并列，便于统一收集）。
HOOK_AUDIT_FILENAME = "hooks_audit.jsonl"

#: `reason` 落盘前的截断长度（脱敏之后）。
REASON_MAX_CHARS = 200

#: 落盘目录覆盖（None = CWD 下 `logs/`）。测试通过 monkeypatch 指向 tmp_path。
AUDIT_DIR: Path | None = None


def _audit_dir() -> Path:
    """审计落盘目录（`Settings` 无 `log_dir` 字段，与 exec/plugin audit 同样兜底）。"""
    if AUDIT_DIR is not None:
        return AUDIT_DIR
    from app.config import settings

    return Path(getattr(settings, "log_dir", "") or "logs")


def _summarize_reason(reason: str) -> str:
    """脱敏 + 截断 hook 返回的原因文本。

    hook 的返回来自沙箱里的第三方代码，可能夹带密钥或非法长度 —— 落盘前必须处理
    （复用全仓统一的脱敏规则集，避免各写一套）。
    """
    text = reason or ""
    try:
        from app.subagent.redact import redact_text

        text, _hits = redact_text(text)
    except Exception:  # noqa: BLE001 — 脱敏不可用不应阻断审计
        pass
    if len(text) > REASON_MAX_CHARS:
        return f"{text[:REASON_MAX_CHARS]}...(truncated)"
    return text


def record_hook(
    *,
    event: str,
    hook: str,
    owner: str,
    tenant: str = "",
    user: str = "",
    session: str = "",
    duration_ms: int | None = None,
    exit_code: int | None = None,
    blocked: bool = False,
    outcome: str = "",
    reason: str | None = None,
) -> None:
    """记录一次 hook 执行（同步写，失败不阻断调用方）。

    Args:
        event: 事件名（不阻断的路径用 `*` 表示"非事件"场合，如启动告警）。
        hook: hook 名。
        owner: `deployment` / `user`（注册来源，决定信任级别）。
        tenant / user / session: 执行时的身份（多租户归属）。
        duration_ms: 执行耗时（毫秒）。
        exit_code: 子进程退出码（被 kill/超时或未启动时为 None）。
        blocked: 本次执行是否**阻断**了工具执行。
        outcome: `ok` / `error` / `timeout` / `blocked` / `register_denied` / ...
        reason: 补充原因（会脱敏 + 截断）。
    """
    if not HOOK_AUDIT_ENABLED:
        return
    try:
        log_dir = _audit_dir()
        log_dir.mkdir(parents=True, exist_ok=True)
        entry: dict[str, Any] = {
            "ts": utc_timestamp(),
            "event": event,
            "hook": hook,
            "owner": owner,
            "tenant": tenant,
            "user": user,
            "session": session,
            "outcome": outcome,
            "blocked": blocked,
            "exit_code": exit_code,
        }
        if duration_ms is not None:
            entry["duration_ms"] = duration_ms
        if reason:
            entry["reason"] = _summarize_reason(reason)
        with (log_dir / HOOK_AUDIT_FILENAME).open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except Exception:  # noqa: BLE001 — 审计失败不影响 hook 执行结果
        logger.warning("hook audit log write failed", exc_info=True)
