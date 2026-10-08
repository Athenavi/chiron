"""插件审计流水（`plugin_audit.jsonl`）—— **可复用模块**，不只服务 `app/api/plugins.py`。

为什么要单独成模块：此前它只是 `app/api/plugins.py` 里的一个**内联写盘**，于是**运行时**拉起
插件进程的两处（`app/mcp/client.py`、`app/skill/manager.py`）想记也记不了 —— 那两处长期没进
执行审计清单就是这么来的（见 `python-engine/tests/test_exec_audit_inventory.py`）。

口径：四条 JSONL 流水（`exec` / `hooks` / `approval` / `plugin`）本就"由**同一套**收集与排障
流程合并读取"，因此：

* `ts` 一律用 `utc_timestamp()`（**UTC + 毫秒**）—— 本地时间/秒级会让跨实例合并排序直接错位；
* 身份一律带 `tenant` / `user` / `session`（与 exec/hooks audit 同源，呼叫方不必逐个透传）。
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

from app.audit_log import utc_timestamp

logger = logging.getLogger(__name__)

#: 开关。与 exec/hook audit 同款：审计是安全能力而非可选项，落盘失败只告警。
PLUGIN_AUDIT_ENABLED = True

#: 审计文件名（与 `exec_audit.jsonl` / `hooks_audit.jsonl` 并列，便于统一收集）
PLUGIN_AUDIT_FILENAME = "plugin_audit.jsonl"

#: 落盘目录覆盖（None = CWD 下 `logs/`）。测试通过 monkeypatch 指向 tmp_path。
AUDIT_DIR: Path | None = None


def _audit_dir() -> Path:
    if AUDIT_DIR is not None:
        return AUDIT_DIR
    from app.config import settings

    return Path(getattr(settings, "log_dir", "") or "logs")


def record_plugin(
    *,
    plugin: str,
    action: str = "execute",
    user_id: str = "",
    duration_ms: int | None = None,
    success: bool = True,
    error: str | None = None,
    input_keys: list[str] | None = None,
) -> None:
    """记一条插件流水（**永不**向调用方抛异常）。

    Args:
        plugin: 插件名（或 MCP server 名 / 命令名）。
        action: 这次是什么动作（`execute` 执行插件代码 / `spawn` 拉起插件进程 …）。
        user_id: 调用方已知的用户 id（缺省时取工具上下文）。
        duration_ms: 耗时（毫秒）。
        success: 是否成功。
        error: 失败原因（成功时省略）。
        input_keys: 输入对象的键名（**只记键名，不记值** —— 值可能带密钥）。
    """
    if not PLUGIN_AUDIT_ENABLED:
        return
    try:
        from app.tools.context import get_session_id, get_tenant_id, get_user_id

        log_dir = _audit_dir()
        log_dir.mkdir(parents=True, exist_ok=True)
        entry: dict[str, Any] = {
            "ts": utc_timestamp(),
            "plugin": plugin,
            "action": action,
            "tenant": get_tenant_id() or "",
            "user": user_id or get_user_id() or "",
            "session": get_session_id() or "",
            "success": bool(success),
        }
        if duration_ms is not None:
            entry["duration_ms"] = duration_ms
        if error:
            entry["error"] = error
        if input_keys is not None:
            entry["input_keys"] = sorted(input_keys)
        with (log_dir / PLUGIN_AUDIT_FILENAME).open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except Exception:  # noqa: BLE001 — 审计失败不影响插件执行结果
        logger.warning("plugin audit log write failed", exc_info=True)
