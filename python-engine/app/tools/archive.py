"""read_archive —— 按 `messages.id` 取回被压缩掉的原文。

与 `read_tool_result` 的分工：

* `read_tool_result` 走 Redis 的 `result_store`，TTL **2 小时**（`result_store.py:18`）——
  会话内够用，隔天就取不回了；
* `read_archive` 走 PG 的 `messages` **持久表**，是"压缩后原文可召回"的长期通道。

两者互补而不是替代：压缩发生时先把原文塞进 `result_store`（快、不落库），
需要长期可召回的内容则靠本工具按消息 id 回查。于是"落盘失败"不再等于"永久丢失"。

**命名避开 `recall`**：它已被记忆检索占用（`app/tools/memory.py`）。
"""

from __future__ import annotations

import logging
from typing import Any

from app.tools.context import get_session_id, get_tenant_id_int
from app.tools.registry import registry

logger = logging.getLogger(__name__)

#: 单次返回上限 64KB —— 分段取是刻意的，否则"取回"等于又把大结果灌回上下文
DEFAULT_LIMIT = 8000
MAX_LIMIT = 64 * 1024


async def read_archive(
    ref: str, offset: int = 0, limit: int = DEFAULT_LIMIT
) -> dict[str, Any]:
    """按消息 id 取回原文片段（支持 offset/limit 分段）。"""
    message_id = (ref or "").strip()
    if not message_id:
        return {"error": "ref is required (a message id)"}
    try:
        start = max(0, int(offset or 0))
        span = int(limit or DEFAULT_LIMIT)
    except (TypeError, ValueError):
        return {"error": "offset/limit must be integers"}
    span = max(1, min(span, MAX_LIMIT))

    from app.agent import history_writer

    found = await history_writer.load_message_content(
        get_session_id(), get_tenant_id_int(), message_id, offset=start, limit=span
    )
    if found is None:
        # 归属不符与"不存在"返回同一句话：区分两者等于告诉调用方"这条 id 存在，
        # 只是不属于你" —— 那是可用来探测他人会话的侧信道。
        return {"error": f"archive not found or not accessible: {message_id}"}

    chunk, total = found
    return {
        "ref": message_id,
        "offset": start,
        "limit": span,
        "returned_chars": len(chunk),
        "total_chars": total,
        "has_more": start + len(chunk) < total,
        "content": chunk,
        "note": "这是原文片段；若还要后面的内容，把 offset 设为 "
        f"{start + len(chunk)} 继续取。",
    }


registry.register(
    name="read_archive",
    description=(
        "Read back the original text of one message by its id, after it was pruned or "
        "summarised out of your context. Use this instead of assuming the content never "
        "existed. Unlike read_tool_result (which expires after a couple of hours), this "
        "reads the durable message store, so it works for older turns too. "
        "Supports offset/limit paging."
    ),
    parameters={
        "type": "object",
        "properties": {
            "ref": {
                "type": "string",
                "description": (
                    "The message id referenced in a pruning notice "
                    "(often 'message' or 'archive ref' followed by a uuid)"
                ),
            },
            "offset": {
                "type": "integer",
                "default": 0,
                "description": "Character offset to start from",
            },
            "limit": {
                "type": "integer",
                "default": DEFAULT_LIMIT,
                "description": f"Max characters to return (capped at {MAX_LIMIT})",
            },
        },
        "required": ["ref"],
    },
    handler=read_archive,
)
