"""run 现场 checkpoint（方案 01 §3.1、评审 01 §一）—— **C1 批 2：只写不读**。

本批只做「建表 + 回合末落盘」，**不接恢复路径**：这样即使写入有问题也不影响对话本身
（写入失败只降级「恢复粒度」，与 workflow 的 `persist_checkpoint` 同一语义）。
恢复（用户重试 / reconciler）在批 3 接入。

三条形状 / 边界约定（评审 01 §1.2、§1.3、§1.5）：

* ``messages``：**尾部窗口（按 token 预算）+ 既有压缩摘要**；下限保证最近 2 个完整回合 ——
  否则 `assistant(tool_calls)` / `tool` 配对会断在快照边上（那正是
  `_ensure_valid_tool_sequence` 要修的情形，不该由 checkpoint 制造）；
* ``done_tools`` / ``replay_pending``：前者是跳过依据；后者是"已开始未结束"的**精确**判定结果
  —— 依据 `tool_calls` 的两段式写入（执行前 INSERT 空 output、执行后 UPDATE，见
  `internal/session/manager.go`），因此不必采用"保守视为已完成"的策略；
* **TTL 两档**：热续跑 **1h**（与 SSE 重放窗口 `sseEventsTTL` 对齐 —— 只有在此窗口内
  "用户无感"才成立）/ 冷恢复 **24h**（允许续跑但不承诺事件补齐），超窗 `abandoned`（可见）。

状态机：`running` / `checkpointed` / `resuming` / `completed` / `failed` / `cancelled` / `abandoned`。
其中 `running → checkpointed` 的判定**不在引擎内部**（故障进程无法自证死亡），由接管方依据
run 租约过期 + 会话运行锁可获取来判定（方案 01 §3.1）。
"""

from __future__ import annotations

import calendar
import json
import logging
import time
import uuid
from typing import Any, TypedDict, cast

logger = logging.getLogger(__name__)

#: 热续跑窗口（秒）：与 SSE 重放窗口对齐（`internal/broadcast/hub.go` 的 sseEventsTTL = 1h）。
#: 只有在此窗口内，"缺口由 Last-Event-ID 补发 ⇒ 用户无感"才成立。
HOT_RESUME_WINDOW_SECONDS = 3600
#: 冷恢复窗口（秒）：允许续跑但**不承诺事件补齐**（前端需重拉消息列表）。
COLD_RESUME_WINDOW_SECONDS = 24 * 3600

STATUS_RUNNING = "running"
STATUS_CHECKPOINTED = "checkpointed"
STATUS_RESUMING = "resuming"
STATUS_COMPLETED = "completed"
STATUS_FAILED = "failed"
STATUS_CANCELLED = "cancelled"
STATUS_ABANDONED = "abandoned"

#: 活跃状态 —— 与唯一索引 `ux_agent_runs_session_active` 的谓词一致
ACTIVE_STATUSES: tuple[str, ...] = (STATUS_RUNNING, STATUS_CHECKPOINTED, STATUS_RESUMING)

#: 快照里 messages 的字节上限（超过则只留尾部窗口）
SNAPSHOT_MESSAGES_MAX_BYTES = 256 * 1024
#: 快照里 messages 的**条数**上限。
#:
#: 必须同时有条数上限：只有字节上限时，"200 条小消息"总字节数远小于 256KB，于是快照等于
#: 整段 —— 那就失去了"尾部窗口"的意义（也会让 checkpoint 行随会话线性膨胀）。
#: 待 C4（模型窗口表）落地后，这里改为按窗口比例计算。
SNAPSHOT_MAX_TAIL_MESSAGES = 40
#: 快照至少保留的条数（保证最近若干条完整，避免把 `assistant(tool_calls)` 与其
#: `tool` 结果切到快照两侧）
SNAPSHOT_MIN_TAIL_MESSAGES = 4


class RunCheckpoint(TypedDict, total=False):
    """回合边界快照。字段语义见模块文档与方案 01 §3.1。"""

    turn_index: int
    messages: list[dict[str, Any]]
    done_tools: list[str]
    replay_pending: list[str]
    pending: dict[str, Any]
    turn_id: str
    usage: dict[str, int]
    last_event_id: str
    instance_id: str
    saved_at: str


def _pool() -> Any:
    """取引擎的 PG 池（`app/db.py` 的兼容层：直连或经网关的统一客户端）。"""
    from app.db import get_pool

    return get_pool()


def build_snapshot(
    *,
    messages: list[dict[str, Any]],
    turn_index: int,
    turn_id: str = "",
    done_tools: list[str] | None = None,
    replay_pending: list[str] | None = None,
    usage: dict[str, int] | None = None,
    last_event_id: str = "",
    instance_id: str = "",
    max_tail_messages: int = SNAPSHOT_MAX_TAIL_MESSAGES,
    min_tail_messages: int = SNAPSHOT_MIN_TAIL_MESSAGES,
) -> RunCheckpoint:
    """构造快照：**尾部窗口 + 既有压缩摘要**（评审 01 §1.2）。

    为什么不是整段：会话消息已有 PG `messages` 表与 Redis 缓存，checkpoint 只需"能重放的最小现场"。

    两道上限**都要**：条数上限保证"尾部窗口"在任何消息尺寸下都成立（只有字节上限时，
    很多条小消息会让快照等于整段）；字节上限挡住单条巨型消息。

    `min_tail_messages` 是**下限**：快照不能把一个回合切一半 —— 保证最近若干条完整保留，
    避免 `assistant(tool_calls)` 与它的 `tool` 结果被分到快照两侧。
    """
    limit = max(min_tail_messages, max_tail_messages)
    tail: list[dict[str, Any]] = []
    used = 0
    for message in reversed(messages):
        size = len(json.dumps(message, ensure_ascii=False).encode("utf-8"))
        if tail and (len(tail) >= limit or used + size > SNAPSHOT_MESSAGES_MAX_BYTES):
            break
        tail.append(message)
        used += size
    tail.reverse()
    if not tail:
        tail = messages[-min_tail_messages:]

    return RunCheckpoint(
        turn_index=turn_index,
        messages=tail,
        done_tools=list(done_tools or []),
        replay_pending=list(replay_pending or []),
        usage=dict(usage or {}),
        turn_id=turn_id,
        last_event_id=last_event_id,
        instance_id=instance_id,
        saved_at=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    )


async def save(
    *,
    tenant_id: str,
    session_id: str,
    checkpoint: RunCheckpoint,
    run_token: str = "",
    instance_id: str = "",
) -> bool:
    """回合末落盘（**异步、失败只降级恢复粒度**）。

    返回是否写入成功 —— 调用方**不该**因为它失败而改变对话行为（与 workflow 的
    `persist_checkpoint` 同一语义：失败只影响"恢复粒度"）。

    写入策略：先 UPDATE 活跃行，没有再 INSERT。并发下 INSERT 可能撞
    `ux_agent_runs_session_active`（同一会话的另一实例刚写了）→ 重试一次 UPDATE。
    """
    if not session_id:
        return False

    payload = json.dumps(checkpoint, ensure_ascii=False)
    now_token = run_token or uuid.uuid4().hex
    try:
        pool = _pool()
        if pool is None:
            return False

        updated = await pool.execute(
            """
            UPDATE agent_runs
               SET checkpoint = $3::jsonb,
                   checkpoint_at = now(),
                   turn_id = $4,
                   run_token = COALESCE(NULLIF($5, ''), run_token),
                   instance_id = COALESCE(NULLIF($6, ''), instance_id),
                   updated_at = now()
             WHERE session_id = $1
               AND ($2 = '' OR tenant_id = $2)
               AND status IN ('running', 'checkpointed', 'resuming')
            """,
            session_id,
            tenant_id or "",
            payload,
            str(checkpoint.get("turn_id", "") or ""),
            now_token,
            instance_id,
        )
        if _rowcount(updated) > 0:
            return True

        await pool.execute(
            """
            INSERT INTO agent_runs
                (id, tenant_id, session_id, turn_id, run_token, status,
                 checkpoint, checkpoint_at, instance_id, created_at, updated_at)
            VALUES ($1, $2, $3, $4, $5, 'running', $6::jsonb, now(), $7, now(), now())
            """,
            uuid.uuid4().hex,
            tenant_id or None,
            session_id,
            str(checkpoint.get("turn_id", "") or "") or None,
            now_token,
            payload,
            instance_id or None,
        )
        return True
    except Exception as exc:  # noqa: BLE001 — 落盘失败不改变对话行为
        logger.warning("checkpoint save failed (session=%s): %s", session_id, exc)
        return False


async def load(*, tenant_id: str, session_id: str) -> RunCheckpoint | None:
    """读最近一次 checkpoint（**批 3 才接线到恢复路径**）。"""
    if not session_id:
        return None
    try:
        pool = _pool()
        if pool is None:
            return None
        row = await pool.fetchrow(
            """
            SELECT checkpoint, checkpoint_at, status
              FROM agent_runs
             WHERE session_id = $1 AND ($2 = '' OR tenant_id = $2)
             ORDER BY updated_at DESC
             LIMIT 1
            """,
            session_id,
            tenant_id or "",
        )
        if row is None:
            return None
        raw = row["checkpoint"]
        if raw is None:
            return None
        parsed = json.loads(raw) if isinstance(raw, str) else dict(raw)
        return cast(RunCheckpoint, parsed)
    except Exception as exc:  # noqa: BLE001
        logger.warning("checkpoint load failed (session=%s): %s", session_id, exc)
        return None


async def mark(*, tenant_id: str, session_id: str, status: str) -> bool:
    """迁移状态（`completed` / `failed` / `cancelled` / `abandoned` …）。

    终态行会被移出活跃集合（唯一索引随之释放），因此同一会话可以开始新 run。
    """
    if not session_id:
        return False
    try:
        pool = _pool()
        if pool is None:
            return False
        result = await pool.execute(
            """
            UPDATE agent_runs
               SET status = $3, updated_at = now()
             WHERE session_id = $1 AND ($2 = '' OR tenant_id = $2)
               AND status IN ('running', 'checkpointed', 'resuming')
            """,
            session_id,
            tenant_id or "",
            status,
        )
        return _rowcount(result) > 0
    except Exception as exc:  # noqa: BLE001
        logger.warning("checkpoint mark failed (session=%s): %s", session_id, exc)
        return False


def resume_window(checkpoint: RunCheckpoint) -> str:
    """按快照的保存时间判定恢复窗口：`hot` / `cold` / `abandoned`。

    这是"用户无感"承诺的**前提**：热窗口内缺口由 SSE 重放补齐；冷窗口内只能续跑、
    不能承诺事件补齐（评审 01 §1.5）。
    """
    saved_at = str(checkpoint.get("saved_at", "") or "")
    if not saved_at:
        return "cold"
    try:
        # 必须用 `calendar.timegm`（把 struct_time 当 **UTC**）而不是 `time.mktime`
        # —— 后者按**本地时区**解释，于是 UTC+8 的机器上 age 会偏差 8 小时，
        # "热窗口"判定直接错位（这个 bug 在测试里才暴露）。
        saved = calendar.timegm(time.strptime(saved_at, "%Y-%m-%dT%H:%M:%SZ"))
    except ValueError:
        return "cold"
    age = time.time() - saved
    if age <= HOT_RESUME_WINDOW_SECONDS:
        return "hot"
    if age <= COLD_RESUME_WINDOW_SECONDS:
        return "cold"
    return "abandoned"


def _rowcount(result: Any) -> int:
    """从 asyncpg 的 `UPDATE n` 状态串里取行数（统一客户端也返回同一形状）。"""
    text = str(result or "")
    if text.startswith("UPDATE "):
        try:
            return int(text.split(" ", 1)[1])
        except (IndexError, ValueError):
            return 0
    return 0
