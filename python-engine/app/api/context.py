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

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from app.context.branch_condense import DEFAULT_KEEP_TAIL, condense_messages
from app.db import get_pool

logger = logging.getLogger(__name__)
router = APIRouter(tags=["context"])

# 源消息读取上限：与 Go 侧复制窗口的语义一致（够用即可，避免一次把超长会话拉进内存）
MAX_SOURCE_MESSAGES = 400

SOURCE_SQL = """
SELECT role, content
  FROM messages
 WHERE session_id::text = $1
 ORDER BY created_at, id
 LIMIT $2
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
async def condense_context(body: CondenseRequest) -> dict:
    """把源会话保留区的历史压成核心上下文摘要，**只返回文本**（不写库）。"""
    try:
        pool = get_pool()
    except RuntimeError as exc:  # 数据库未初始化：无法读历史，明确 503 而不是编造摘要
        logger.error("condense unavailable, db not initialized: %s", exc)
        raise HTTPException(status_code=503, detail="database unavailable") from exc

    try:
        rows = await pool.fetch(SOURCE_SQL, body.session_id, MAX_SOURCE_MESSAGES)
    except Exception as exc:  # noqa: BLE001 - 查询失败（含非法 id）都按 400 处理
        logger.warning("condense source query failed: session=%s error=%s", body.session_id, exc)
        raise HTTPException(status_code=400, detail="cannot read source session") from exc

    # 只压到分叉点为止：分叉点之后的内容属于"被裁掉"（除非显式要求 include_future）
    source = [dict(row) for row in rows][: body.from_index]
    future = [dict(row) for row in rows][body.from_index :] if body.include_future else []

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
