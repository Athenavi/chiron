"""上下文压缩 API（内网接口，由网关调用）。

    POST /v1/context/condense

**分工**（与 `app/queue/worker.py` 里 `_handle_agent_followup` 的注释一致）：
`messages` 落库、turn 状态、SSE 推送都在 Go。引擎只做"读历史 → 调模型 → 返回摘要"，
网关拿到摘要后负责：把它写成新会话里的 `role='system'`、`source='branch_summary'` 消息，
并把 `branch_state` 从 `pending` 改成 `ready` / `failed`。

这样两端不会同时写同一张表，也让"压缩"这件事可以独立测试与复用。
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from app.context.branch_condense import DEFAULT_KEEP_TAIL, condense_messages
from app.db import get_pool

logger = logging.getLogger(__name__)
router = APIRouter(tags=["context"])

# 源消息读取上限：一次压缩最多读多少条，避免把超长会话整体拉进内存。
MAX_SOURCE_MESSAGES = 400

# 分叉点之前的最后 N 条（倒序取、应用层翻回升序）。
#
# 为什么不是"最早的 N 条"：压缩的目的是让新会话"记得"它继承了什么。超长会话里
# 最早的 400 条往往全是开场寒暄，最近的进展反而被丢掉 —— 摘要会退化成
# "聊了很多但没有结论"，而上下文继承恰恰要的是最近在做什么、卡在哪。
# 因此窗口取**分叉点往回数**的最近 N 条，而不是从会话开头数。
SOURCE_HEAD_SQL = """
SELECT role, content
  FROM (SELECT role, content,
               ROW_NUMBER() OVER (ORDER BY created_at, id) AS rn
          FROM messages
         WHERE session_id::text = $1) g
 WHERE rn <= $2
 ORDER BY rn DESC
 LIMIT $3
"""

# 分叉点之后的前 N 条（只在 include_future 为真时读）。
SOURCE_FUTURE_SQL = """
SELECT role, content
  FROM (SELECT role, content,
               ROW_NUMBER() OVER (ORDER BY created_at, id) AS rn
          FROM messages
         WHERE session_id::text = $1) g
 WHERE rn > $2
 ORDER BY rn
 LIMIT $3
"""


class CondenseRequest(BaseModel):
    session_id: str = Field(..., min_length=1, description="源会话：读它的历史来压缩")
    from_index: int = Field(..., gt=0, description="分叉点：保留到源会话的第几条消息（含）")
    keep_tail: int = Field(
        DEFAULT_KEEP_TAIL, ge=0, description="最近多少条留原文（它们不进压缩区）"
    )
    include_future: bool = Field(
        False, description="是否把被裁掉的后段也压成一句'后续走向'（默认否）"
    )
    target_session_id: str = Field(
        "", description="摘要将写往的会话（仅用于日志与追溯，本端点不写库）"
    )


@router.post("/v1/context/condense")
async def condense_context(body: CondenseRequest) -> dict[str, Any]:
    """把源会话保留区的历史压成核心上下文摘要，**只返回文本**（不写库）。"""
    try:
        pool = get_pool()
    except RuntimeError as exc:  # 数据库未初始化：无法读历史，明确 503 而不是编造摘要
        logger.error("condense unavailable, db not initialized: %s", exc)
        raise HTTPException(status_code=503, detail="database unavailable") from exc

    # 分叉点之前的最近 N 条（倒序取再翻正），不是"会话最早的 N 条"。
    # 超长会话里最早的几百条往往全是开场，最近的进展会被挤掉 —— 那样压出来的摘要
    # 记不住"现在在做什么"，而上下文继承要的正是这一点。
    try:
        head_rows = await pool.fetch(
            SOURCE_HEAD_SQL, body.session_id, body.from_index, MAX_SOURCE_MESSAGES
        )
    except Exception as exc:  # noqa: BLE001 - 查询失败（含非法 id）都按 400 处理
        logger.warning("condense source query failed: session=%s error=%s", body.session_id, exc)
        raise HTTPException(status_code=400, detail="cannot read source session") from exc

    # 倒序取到了"最近 N 条"，翻回升序喂给压缩函数（它按时间先后组织摘要）
    source = [dict(row) for row in reversed(head_rows)]

    future: list[dict[str, Any]] = []
    if body.include_future:
        try:
            future_rows = await pool.fetch(
                SOURCE_FUTURE_SQL, body.session_id, body.from_index, MAX_SOURCE_MESSAGES
            )
            future = [dict(row) for row in future_rows]
        except Exception as exc:  # noqa: BLE001 - 后段只是锦上添花，读不到就退回纯前段摘要
            logger.warning(
                "condense future query failed: session=%s error=%s", body.session_id, exc
            )

    gateway = None
    try:
        from app.main import get_gateway  # 局部导入：避免 main ←→ api 的循环导入

        gateway = await get_gateway()
    except Exception as exc:  # noqa: BLE001 - 拿不到 gateway 就走提取式降级，不失败
        logger.warning("condense: gateway unavailable, will degrade: %s", exc)

    result = await condense_messages(
        messages=source,
        gateway=gateway,
        keep_tail=body.keep_tail,
        include_future=body.include_future,
        future_messages=future,
    )
    logger.info(
        "condense done: src=%s target=%s from_index=%s keep_tail=%s condensed=%s degraded=%s chars=%d",
        body.session_id,
        body.target_session_id,
        body.from_index,
        body.keep_tail,
        result.condensed,
        result.degraded,
        len(result.summary),
    )
    return {
        "summary": result.summary,
        "condensed": result.condensed,
        "degraded": result.degraded,
        "reason": result.reason,
        "source_messages": result.source_messages,
        "usage": result.usage,
    }
