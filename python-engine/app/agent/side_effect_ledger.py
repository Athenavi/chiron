"""副作用账本 —— 一次会话到底改动了什么，哪些可回滚、哪些不可。

**为什么需要它**

`/undo` 现在能真正回滚文件写入（`app/agent/undo_stack.py`），但它只覆盖"文件写入"一类：
`shell_exec` 里的删除、浏览器操作、MCP 外部调用都不在里面。而外部副作用**本质上不可回滚**
（发出去的请求收不回来）—— 能做的只有两件事：

1. **事前说清**：确认卡片上标明"此操作不可撤销"；
2. **事后记账**：留下"何时、动了什么、结果如何"的清单，供排障与审计。

本模块是这两件事的共同底座。原则与 `undo_stack` 一致：**诚实优先** —— 撤不回就明说，
而不是等用户敲 `/undo` 才发现什么都没发生。
"""

from __future__ import annotations

import logging
import time
from dataclasses import asdict, dataclass

logger = logging.getLogger(__name__)

#: 每个会话保留的账目条数（是"最近发生了什么"，不是审计归档）
MAX_ENTRIES = 200
#: 账目保留时长（秒）
TTL_SECONDS = 24 * 60 * 60

#: 副作用类别（与动作分级 read/write/delete/external 对齐）
KIND_FILE_WRITE = "file_write"
KIND_DELETE = "delete"
KIND_EXTERNAL = "external_call"

#: 可回滚性
ROLLBACK_AUTO = "auto"   # 有快照，可用 /undo 恢复
ROLLBACK_NONE = "none"   # 做不到 —— 必须在**批准前**讲清楚

#: 这些工具的副作用有快照兜底（见 app/agent/undo_stack.py 的挂钩点）
_SNAPSHOT_TOOLS = frozenset({"write_file", "edit_file"})

#: target 摘要里优先取这几个参数（路径 / 地址 / 命令）
_TARGET_KEYS = ("path", "url", "command", "cmd", "file", "target", "query")


@dataclass
class SideEffect:
    """一条副作用记录。"""

    kind: str          # file_write / delete / external_call
    tool: str          # 产生它的工具名
    target: str        # 路径 / 地址 / 命令摘要（供人看）
    rollback: str      # auto（可 /undo）| none（不可撤销）
    at: float
    detail: str = ""   # ok / error

    def describe(self) -> str:
        flag = "可撤销" if self.rollback == ROLLBACK_AUTO else "不可撤销"
        return f"{self.tool} → {self.target}（{flag}）"


def kind_for(level: str, tool: str) -> str:
    """动作级别 → 账目类别。"""
    from app.agent.tool_policy import DELETE, EXTERNAL, WRITE

    if level == EXTERNAL:
        return KIND_EXTERNAL
    if level == DELETE:
        return KIND_DELETE
    if level == WRITE:
        return KIND_FILE_WRITE
    return ""


def rollback_capability(level: str, tool: str) -> str:
    """这个副作用能不能撤销。

    * 文件写入（`write_file` / `edit_file`）→ ``auto``：写入前有快照，`/undo` 能恢复；
    * 其余一律 ``none``：`shell_exec` 里的删除拦不到、`job_kill` 不可逆、外部调用收不回来。
      **不猜、不承诺** —— 撤不回的一律说实话。
    """
    return ROLLBACK_AUTO if tool in _SNAPSHOT_TOOLS else ROLLBACK_NONE


def target_of(args: dict | None, tool: str) -> str:
    """从参数里取一个"人看得懂的目标摘要"。"""
    if not isinstance(args, dict):
        return tool
    for key in _TARGET_KEYS:
        value = args.get(key)
        if isinstance(value, str) and value.strip():
            text = " ".join(value.split())
            return text[:200]
    return tool


async def record(session_id: str, effect: SideEffect) -> None:
    """记一条账。失败只记日志 —— 账本不是关键路径，不该影响工具执行。"""
    try:
        import json

        from app.redis_client import get_redis

        redis = await get_redis()
        if redis is None:
            return
        key = _key(session_id)
        await redis.lpush(key, json.dumps(asdict(effect), ensure_ascii=False))
        await redis.ltrim(key, 0, MAX_ENTRIES - 1)
        await redis.expire(key, TTL_SECONDS)
    except Exception as e:  # noqa: BLE001
        logger.warning("side effect ledger record failed: %s", e)


async def recent(session_id: str, limit: int = 50) -> list[SideEffect]:
    """读回最近的账目（新的在前）。"""
    try:
        from app.redis_client import get_redis

        redis = await get_redis()
        if redis is None:
            return []
        items = await redis.lrange(_key(session_id), 0, max(0, limit - 1))
    except Exception as e:  # noqa: BLE001
        logger.warning("side effect ledger read failed: %s", e)
        return []
    out: list[SideEffect] = []
    for raw in items or []:
        effect = _decode(raw)
        if effect is not None:
            out.append(effect)
    return out


def _key(session_id: str) -> str:
    from app.redis_keys import rkey

    return rkey(f"side_effects:{session_id or 'nosession'}")


def _decode(raw: object) -> SideEffect | None:
    import json

    if isinstance(raw, (bytes, bytearray)):
        raw = raw.decode("utf-8", "replace")
    try:
        data = json.loads(str(raw))
    except Exception:  # noqa: BLE001 - 损坏条目跳过
        return None
    if not isinstance(data, dict) or not data.get("tool"):
        return None
    return SideEffect(
        kind=str(data.get("kind", "")),
        tool=str(data.get("tool", "")),
        target=str(data.get("target", "")),
        rollback=str(data.get("rollback", ROLLBACK_NONE)),
        at=float(data.get("at", 0.0)),
        detail=str(data.get("detail", "")),
    )


def confirmation_warning(level: str, tool: str) -> str:
    """确认卡片上的**事前**风险提示（空串 = 没什么要额外讲的）。

    只对"不可撤销"的操作加提示 —— 满屏警告等于没有警告，只有真正撤不回的才值得占用注意。
    """
    if rollback_capability(level, tool) == ROLLBACK_NONE and kind_for(level, tool):
        return "，此操作不可撤销"
    return ""
