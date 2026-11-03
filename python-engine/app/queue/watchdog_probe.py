"""Watchdog 的数据面：把"正在跑的回合"摘要成 :class:`app.agent.watchdog.Sample`。

**为什么单独一层**

判断本身是纯函数（`app/agent/watchdog.py`），它不该知道表名、连接池与租户过滤 ——
否则"已知兜圈子样本"就没法在测试里回放。本模块只做两件事：读，然后摘要。

**只取摘要，不取原文**

`messages.content` 与 `tool_calls.input` 可能很长（一次工具调用能带整段文件内容）。
watchdog 每几分钟跑一次、同时看多个回合，把原文搬进内存与 Redis 既无必要也放大泄露面；
判断"有没有变化"只需要能比较的指纹。

**fail-closed**

连接池不可用、单表缺失、查询报错 —— 一律返回空列表。watchdog 的缺省语义是"不干预"，
读不到东西就应当什么都不说，而不是报一个"某处可能卡住"的猜测。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from app.agent.watchdog import Sample, digest

logger = logging.getLogger(__name__)

#: 每个回合每次采样读多少条消息 / 多少次工具调用。
#: 取 20：足够覆盖"最后一轮"的产出，又不至于把一大段历史反复哈希。
SAMPLE_WINDOW = 20

#: 一次 tick 最多看多少个 running 回合。
#: 有上限才有可预测的成本：异常数据让 `turns` 里堆满 `running` 时，这里不会变成全表扫。
MAX_TURNS = 50


@dataclass(frozen=True)
class TurnSample:
    """一拍观测 + 判断器用不到的归属信息（审计需要）。"""

    sample: Sample
    tenant_id: int = 0
    user_id: str = ""
    session_id: str = ""


async def collect(limit: int = MAX_TURNS) -> list[TurnSample]:
    """采集当前仍在运行的回合。任何失败都退化为空列表（见模块 docstring）。"""
    if limit <= 0:
        return []
    try:
        from app.db import get_pool

        pool = get_pool()
    except Exception as exc:  # noqa: BLE001 - 未初始化/配置缺失都按"没有可看的回合"
        logger.debug("watchdog probe: db unavailable (%s)", exc)
        return []

    try:
        rows = await pool.fetch(
            """
            SELECT t.id, t.session_id, t.status, t.user_id, s.tenant_id
            FROM turns t
            JOIN sessions s ON s.id = t.session_id
            WHERE t.status = 'running' AND t.session_id IS NOT NULL
            ORDER BY t.started_at DESC NULLS LAST
            LIMIT $1
            """,
            limit,
        )
    except Exception as exc:  # noqa: BLE001 - 迁移未执行/表不存在时同样只是"看不了"
        logger.warning("watchdog probe: turn scan failed: %s", str(exc)[:200])
        return []

    out: list[TurnSample] = []
    for row in rows or []:
        turn_id = str(row["id"] or "")
        session_id = str(row["session_id"] or "")
        if not turn_id or not session_id:
            continue
        try:
            sample = await _sample_turn(
                pool,
                turn_id=turn_id,
                session_id=session_id,
                turn_status=str(row["status"] or ""),
            )
        except Exception as exc:  # noqa: BLE001 - 单个回合读失败不该拖垮整次采集
            logger.debug("watchdog probe: turn %s skipped: %s", turn_id, str(exc)[:160])
            continue
        out.append(
            TurnSample(
                sample=sample,
                tenant_id=_as_int(row["tenant_id"]),
                user_id=str(row["user_id"] or ""),
                session_id=session_id,
            )
        )
    return out


async def _sample_turn(
    pool: Any, *, turn_id: str, session_id: str, turn_status: str
) -> Sample:
    """把一个回合折成摘要。两个查询都给 `session_id` 兜底条件。

    `turn_id` 已经足够定位（有索引），但会话条件不能省：一旦某一版写入把
    `turn_id` 写错，只按它取会把**另一个会话**的内容算进这个回合的进展判断。
    """
    msgs = await pool.fetch(
        """
        SELECT role, content
        FROM messages
        WHERE turn_id = $1 AND session_id = $2
        ORDER BY seq DESC NULLS LAST
        LIMIT $3
        """,
        turn_id,
        session_id,
        SAMPLE_WINDOW,
    )
    calls = await pool.fetch(
        """
        SELECT tool_name, COALESCE(input::text, '') AS args
        FROM tool_calls
        WHERE turn_id = $1 AND session_id = $2
        ORDER BY created_at DESC NULLS LAST
        LIMIT $3
        """,
        turn_id,
        session_id,
        SAMPLE_WINDOW,
    )

    # 消息按 seq DESC 取尾，翻回时间顺序：hash 对顺序敏感，方向必须稳定。
    ordered = list(reversed(list(msgs or [])))
    return Sample(
        turn_id=turn_id,
        turn_status=turn_status,
        content_digest=digest(
            f"{row['role']}\x1f{row['content']}" for row in ordered
        )
        if ordered
        else "",
        tool_digest=digest(
            f"{row['tool_name']}\x1f{row['args']}" for row in (calls or [])
        ),
        message_count=len(ordered),
    )


def _as_int(value: Any) -> int:
    """`tenant_id` 是 smallint；Driver 在不同配置下可能给 str，统一归一。"""
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0
