"""read_tool_result —— 取回被结构摘要替换掉的工具结果原文。

与 ``read_subagent_result`` 同构：当某个工具结果超过阈值时，进上下文的只是一段**结构摘要** +
``result_ref``；细节由本工具按需分段取回。于是"大结果"从「截断即丢失」变成「可查」——
这也是模型不必重复调用同一个工具去拿同样内容的前提。
"""

from __future__ import annotations

import logging
from typing import Any

from app.tools.context import get_session_id
from app.tools.registry import registry

logger = logging.getLogger(__name__)

#: 单次返回上限：**分段取是刻意的** —— 否则"取回"就等于又一次把大结果灌进上下文
DEFAULT_LIMIT = 8000
MAX_LIMIT = 32000


async def read_tool_result(
    result_ref: str, offset: int = 0, limit: int = DEFAULT_LIMIT
) -> dict[str, Any]:
    """取回先前工具结果的原文（支持 offset/limit 分段）。"""
    ref = (result_ref or "").strip()
    if not ref:
        return {"error": "result_ref is required"}
    try:
        start = max(0, int(offset or 0))
        span = int(limit or DEFAULT_LIMIT)
    except (TypeError, ValueError):
        return {"error": "offset/limit must be integers"}
    span = max(1, min(span, MAX_LIMIT))

    from app.agent import result_store

    text = await result_store.load(get_session_id(), ref, offset=start, limit=span)
    if text is None:
        return {
            "error": (
                f"result not found or expired: {ref}"
                "（工具结果只保存 2 小时；过期后需要重新执行原操作）"
            )
        }
    return {
        "result_ref": ref,
        "offset": start,
        "limit": span,
        "returned_chars": len(text),
        "content": text,
        "note": "这是原文片段；若还要后面的内容，增大 offset 继续取。",
    }


registry.register(
    name="read_tool_result",
    description=(
        "Read back the full text of a previous tool result that was replaced by a structural "
        "summary (such a summary carries a result_ref). Use this instead of re-running the same "
        "tool to obtain the same content. Supports offset/limit paging."
    ),
    parameters={
        "type": "object",
        "properties": {
            "result_ref": {
                "type": "string",
                "description": "result_ref printed in the summary trailer of an oversized result",
            },
            "offset": {"type": "integer", "default": 0, "description": "Character offset to start from"},
            "limit": {"type": "integer", "default": DEFAULT_LIMIT, "description": "Max characters to return"},
        },
        "required": ["result_ref"],
    },
    handler=read_tool_result,
)
