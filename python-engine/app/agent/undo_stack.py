"""文件写入的撤销栈 —— ``/undo`` 的真实后端。

**为什么需要它**

``/undo`` 此前是空壳：``app/commands/builtins.py`` 的 ``_undo`` 只做
``ctx.metadata.pop("last_edit")`` 然后回显一个字符串 —— **文件没有任何变化**。
用户以为撤销了，其实没有。这比"没有 /undo"更危险（详见
``docs/agent-safety-and-reliability.md`` §4.2）。

**做法**：在**写入前**保存原内容快照（会话级、有界、TTL），``/undo`` 据此真正恢复。
两条原则：

1. **诚实优先**：快照存不下（文件太大 / 存储不可用）时，工具结果里**明确告知"本次不可撤销"**，
   而不是事后才发现撤不回；
2. **不越界**：恢复前校验目标仍在工作区内 —— 快照存在 Redis，不能假设它没被篡改。
"""

from __future__ import annotations

import logging
import time
from dataclasses import asdict, dataclass
from pathlib import Path

logger = logging.getLogger(__name__)

#: 每个会话保留的快照条数（撤销是"最近几步"的需求，不是版本控制）
MAX_SNAPSHOTS = 20
#: 快照保留时长（秒）
TTL_SECONDS = 24 * 60 * 60
#: 单条快照的内容上限：超过就不存 —— 但要**明确告知**这次写入不可撤销
MAX_SNAPSHOT_BYTES = 256 * 1024


@dataclass
class Snapshot:
    """一次写入前的文件状态。"""

    path: str          # 绝对路径（沙箱内，由 safe_join 产出）
    existed: bool      # 写入前是否存在；False = 新建 → undo 时应当删除它
    content: str       # 写入前的内容（existed=False 时为空串）
    tool: str          # 产生快照的工具名（write_file / edit_file）
    at: float          # 时间戳

    def describe(self) -> str:
        size = len(self.content.encode("utf-8"))
        return f"{self.tool} 前的 {self.path}" + (f"（{size} 字节）" if self.existed else "（此前不存在）")


def _key(session_id: str) -> str:
    from app.redis_keys import rkey

    return rkey(f"undo:{session_id or 'nosession'}")


async def push(session_id: str, snapshot: Snapshot) -> bool:
    """压入一条快照（LIFO）。返回是否成功 —— 失败意味着这次写入无法撤销。"""
    try:
        from app.redis_client import get_redis

        redis = await get_redis()
        if redis is None:
            return False
        key = _key(session_id)
        import json

        await redis.lpush(key, json.dumps(asdict(snapshot), ensure_ascii=False))
        await redis.ltrim(key, 0, MAX_SNAPSHOTS - 1)  # 有界：只留最近 N 步
        await redis.expire(key, TTL_SECONDS)
        return True
    except Exception as e:  # noqa: BLE001 - 快照失败不该阻断写入本身
        logger.warning("undo snapshot push failed: %s", e)
        return False


async def pop(session_id: str) -> Snapshot | None:
    """弹出最近一条快照；没有 / 存储不可用返回 None。"""
    try:
        from app.redis_client import get_redis

        redis = await get_redis()
        if redis is None:
            return None
        raw = await redis.lpop(_key(session_id))
    except Exception as e:  # noqa: BLE001
        logger.warning("undo snapshot pop failed: %s", e)
        return None
    if raw is None:
        return None
    return _decode(raw)


async def peek(session_id: str, limit: int = 10) -> list[Snapshot]:
    """查看可撤销的步骤（最近的在前）—— 供 `/undo` 无参数时给出提示。"""
    try:
        from app.redis_client import get_redis

        redis = await get_redis()
        if redis is None:
            return []
        items = await redis.lrange(_key(session_id), 0, max(0, limit - 1))
    except Exception as e:  # noqa: BLE001
        logger.warning("undo snapshot peek failed: %s", e)
        return []
    out: list[Snapshot] = []
    for raw in items or []:
        snapshot = _decode(raw)
        if snapshot is not None:
            out.append(snapshot)
    return out


def _decode(raw: object) -> Snapshot | None:
    import json

    if isinstance(raw, (bytes, bytearray)):
        raw = raw.decode("utf-8", "replace")
    try:
        data = json.loads(str(raw))
    except Exception:  # noqa: BLE001 - 快照损坏按"无快照"处理
        return None
    if not isinstance(data, dict) or not data.get("path"):
        return None
    return Snapshot(
        path=str(data.get("path", "")),
        existed=bool(data.get("existed", False)),
        content=str(data.get("content", "")),
        tool=str(data.get("tool", "")),
        at=float(data.get("at", 0.0)),
    )


def restore(snapshot: Snapshot) -> str:
    """按快照恢复文件；返回人类可读的结果描述。

    **校验目标仍在工作区内**：快照存在 Redis（可能被篡改），不能据此写任意路径。
    """
    from app.tools.sandbox import workspace_dir

    path = Path(snapshot.path)
    try:
        base = workspace_dir().resolve()
        if not path.resolve().is_relative_to(base):
            return f"refused: {path} is outside the workspace"
    except Exception as e:  # noqa: BLE001 - 校验不可完成时不冒险
        return f"refused: cannot verify workspace boundary ({e})"

    try:
        if snapshot.existed:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(snapshot.content, encoding="utf-8")
            size = len(snapshot.content.encode("utf-8"))
            return f"restored {path}（{size} 字节）"
        if path.exists():
            path.unlink()
            return f"removed {path}（它是那次写入新建的）"
        return f"{path} 已不存在（无需处理）"
    except Exception as e:  # noqa: BLE001
        logger.warning("undo restore failed (%s): %s", path, e)
        return f"failed to restore {path}: {e}"


async def snapshot_before_write(session_id: str, target: Path, tool: str) -> str:
    """写入前保存快照。返回空串（已保存）或"本次不可撤销"的原因说明。

    调用方应把返回的说明**放进工具结果** —— 撤销不了的事必须当场讲清楚。
    """
    try:
        if not target.exists():
            ok = await push(
                session_id, Snapshot(path=str(target), existed=False, content="", tool=tool, at=time.time())
            )
            return "" if ok else "this write will NOT be undoable (snapshot store unavailable)"
        raw = target.read_bytes()
        if len(raw) > MAX_SNAPSHOT_BYTES:
            return (
                f"this write will NOT be undoable: existing file exceeds "
                f"{MAX_SNAPSHOT_BYTES // 1024}KB snapshot limit"
            )
        ok = await push(
            session_id,
            Snapshot(
                path=str(target),
                existed=True,
                content=raw.decode("utf-8", errors="replace"),
                tool=tool,
                at=time.time(),
            ),
        )
        return "" if ok else "this write will NOT be undoable (snapshot store unavailable)"
    except Exception as e:  # noqa: BLE001 - 快照失败不该阻断写入
        logger.warning("undo snapshot failed (%s): %s", target, e)
        return f"this write will NOT be undoable ({e})"
