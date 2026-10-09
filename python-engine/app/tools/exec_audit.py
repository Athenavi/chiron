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

import asyncio
import json
import logging
import os
import socket
from collections import deque
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
#: 被显式取消（后台任务被 kill / 调用方 task.cancel）——与"失败"区分：
#: 取消是**有人主动终止**，出错排查时两者的处置完全不同。
OUTCOME_CANCELLED = "cancelled"


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
    tenant: str | None = None,
    user: str | None = None,
    session: str | None = None,
) -> None:
    """记录一次执行（同步写，失败不阻断调用方）。

    身份默认从 tool context（contextvars）取 —— 与工具链其它地方同源，调用方不必透传，
    也就不会出现"某条调用路径忘了带身份"的漏记。

    Args:
        tool: 工具名（`shell_exec` / `run_code` / `persistent_shell` / `hook_command` …）。
        command: 命令或代码文本（内部会脱敏 + 截断）。
        outcome: `ok` / `blocked` / `timeout` / `error`。
        exit_code: 进程退出码（被拦下或无法判定时为 None）。
        reason: 被拦下的原因（`outcome=blocked` 时给出）。
        duration_ms: 耗时（毫秒）。
        tenant / user / session: **显式身份覆盖**（`None` = 从 contextvars 取）。
            存在的理由：hook 的 `command` 形态由 `SessionStart` / `UserPromptSubmit`
            **也**会触发，而这两个时点早于 runtime 的 `set_tool_context` —— 那时
            contextvars 是空的，只靠默认来源会把身份记成空串（"谁执行的"就查不到了）。
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
            "tenant": get_tenant_id() if tenant is None else tenant,
            "user": get_user_id() if user is None else user,
            "session": get_session_id() if session is None else session,
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
        _enqueue_for_shipping(entry)
    except Exception:  # noqa: BLE001 — 审计失败不影响执行结果
        logger.warning("exec audit log write failed", exc_info=True)


# ── 多副本集中摄取（N4）────────────────────────────────────────────────────
#
# 多副本部署里，每个引擎实例（以及 S5e 的独立沙箱服务）各有自己的落盘目录 ——
# 排障时"只看得见本机那份"。开启后（`EXEC_AUDIT_SHIP_URL`）每条记录除写盘外还进一个
# **有界队列**，由后台任务成批 POST 到网关 `/v1/internal/audit/exec`，统一落进审计流。
#
# 三条纪律：
#   1. **默认关**：未配置 URL 就完全不参与，零行为变化；
#   2. **不阻断执行**：入队同步且非阻塞；发送失败只告警 + 有限重试，超限丢弃并计数，
#      **绝不向调用方抛错**（与写盘失败同一语义）；
#   3. **可测**：`flush_ship_queue()` 让用例不必等后台任务的节拍。

#: 开启集中摄取的环境变量（值为网关端点，如 `http://gateway:8080/v1/internal/audit/exec`）
SHIP_URL_ENV = "EXEC_AUDIT_SHIP_URL"
#: 单批最多条数（与网关侧 execAuditMaxBatch 对齐，留余量）
SHIP_BATCH_MAX = 100
#: 队列上限：满了丢**最旧**（审计宁可丢旧也不丢新，且必须有计数可观测）
SHIP_QUEUE_MAX = 1000
#: 单批最多尝试次数（失败后退避重试，超限丢弃）
SHIP_MAX_ATTEMPTS = 3
SHIP_TIMEOUT_SECONDS = 5.0

_ship_queue: deque[dict[str, Any]] = deque(maxlen=SHIP_QUEUE_MAX)
_ship_task: asyncio.Task[None] | None = None
#: 观测计数（丢弃 = 队列满或重试超限）
SHIP_STATS: dict[str, int] = {"enqueued": 0, "sent": 0, "dropped": 0, "failed_batches": 0}


def ship_enabled() -> bool:
    """是否开启集中摄取（默认关）。"""
    return EXEC_AUDIT_ENABLED and bool(os.getenv(SHIP_URL_ENV, "").strip())


def _ship_payload(entry: dict[str, Any]) -> dict[str, Any]:
    """把落盘条目映射成网关契约（字段名与落盘不同，映射只在这一处）。"""
    return {
        "tenant_id": entry.get("tenant", ""),
        "user_id": entry.get("user", ""),
        "session_id": entry.get("session", ""),
        "tool": entry.get("tool", ""),
        "command": entry.get("command", ""),
        "outcome": entry.get("outcome", ""),
        "exit_code": entry.get("exit_code"),
        "reason": entry.get("reason", ""),
        "duration_ms": entry.get("duration_ms") or 0,
        "ts": entry.get("ts", ""),
        "instance": socket.gethostname(),
    }


def _enqueue_for_shipping(entry: dict[str, Any]) -> None:
    """入队 + 确保后台任务在跑。**无事件循环时静默跳过**（例如同步调用方）。"""
    global _ship_task
    if not ship_enabled():
        return
    if len(_ship_queue) == _ship_queue.maxlen:
        SHIP_STATS["dropped"] += 1  # deque(maxlen) 会丢最旧
    _ship_queue.append(_ship_payload(entry))
    SHIP_STATS["enqueued"] += 1
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        logger.debug("exec audit ship: no running loop, record only on disk")
        return
    if _ship_task is None or _ship_task.done():
        _ship_task = loop.create_task(_ship_loop())


def _drain(limit: int) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    while _ship_queue and len(out) < limit:
        out.append(_ship_queue.popleft())
    return out


async def _post_batch(batch: list[dict[str, Any]]) -> bool:
    """POST 一批；失败返回 False（**不抛错**）。"""
    url = os.getenv(SHIP_URL_ENV, "").strip()
    if not url:
        return False
    from app.config import settings

    token = getattr(settings, "internal_token", "") or os.getenv("INTERNAL_TOKEN", "")
    try:
        import httpx

        async with httpx.AsyncClient(timeout=SHIP_TIMEOUT_SECONDS) as client:
            resp = await client.post(
                url, json={"records": batch}, headers={"X-Internal-Token": token}
            )
            if resp.is_success:
                return True
            logger.warning(
                "exec audit ship failed: status=%s body=%s", resp.status_code, resp.text[:200]
            )
            return False
    except Exception as exc:  # noqa: BLE001 — 发货失败不影响执行，也不影响落盘
        logger.warning("exec audit ship transport error: %s", exc)
        return False


async def _ship_loop() -> None:
    """后台发货：成批取、有限重试、超限丢弃。**任何异常都不得逸出**（旁路任务）。"""
    while True:
        batch = _drain(SHIP_BATCH_MAX)
        if not batch:
            return
        for attempt in range(1, SHIP_MAX_ATTEMPTS + 1):
            if await _post_batch(batch):
                SHIP_STATS["sent"] += len(batch)
                break
            if attempt < SHIP_MAX_ATTEMPTS:
                await asyncio.sleep(0.5 * attempt)
        else:
            SHIP_STATS["failed_batches"] += 1
            SHIP_STATS["dropped"] += len(batch)
            logger.warning(
                "exec audit ship dropped %d records after %d attempts",
                len(batch),
                SHIP_MAX_ATTEMPTS,
            )


async def flush_ship_queue() -> int:
    """把当前队列发完（用例 / 关停用）；返回**成功发送**的条数。

    与后台任务共用 `_post_batch`，但**不做重试** —— 调用方（用例）要的是确定结果。
    """
    sent = 0
    while True:
        batch = _drain(SHIP_BATCH_MAX)
        if not batch:
            return sent
        if await _post_batch(batch):
            sent += len(batch)
            SHIP_STATS["sent"] += len(batch)
        else:
            SHIP_STATS["failed_batches"] += 1
            SHIP_STATS["dropped"] += len(batch)
