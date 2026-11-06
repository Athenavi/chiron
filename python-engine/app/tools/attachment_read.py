"""attachment_read —— 按 `asset_id` 分段读取附件正文。

**为什么需要这个工具**：附件不再把内容带进历史（见 `app/media/attachments.py` 的
`attachment_reference_block` 与 `runtime.fold_attachment_parts_for_persistence`）。
本轮进上下文的只是**预览**（`MAX_ATTACHMENT_PREVIEW_CHARS`），引用块里只有
`<attachment id="…" />`。没有取回入口时，"历史里发过的文件"就再也读不到了 ——
模型只能让用户重发，而用户会认为这功能坏了。

**为什么按段返回**：取回若一次性把全文灌进上下文，就等于把"驻留"换个名字又做一遍。
上限与 `read_tool_result` 对齐（默认 8000、最多 32000 字符），超出的部分靠 offset 续读。

**归属**：身份取自工具上下文（服务端已鉴权的任务），不是参数 —— 模型不能通过传参
"以别人的身份"取字节。网关按 `tenant_id`/`user_id` 查询谓词过滤，不匹配一律 404。
"""

from __future__ import annotations

import logging
from typing import Any

from app.tools.context import get_tenant_id_int, get_user_id
from app.tools.registry import registry

logger = logging.getLogger(__name__)

#: 单次返回上限：与 `read_tool_result` 同一口径（分段取是刻意的，不是保守）。
DEFAULT_LIMIT = 8000
MAX_LIMIT = 32000


async def attachment_read(
    asset_id: str, offset: int = 0, limit: int = DEFAULT_LIMIT, query: str = ""
) -> dict[str, Any]:
    """读取附件正文的一段（支持 offset/limit 分页）。

    传了 ``query`` 且该附件已建好结构化索引时，改为**检索式读取**（在索引上提问，
    返回答案 + 页码引用），这比线性翻页更适合几十页的 PDF；索引未就绪或类型不支持时
    自动回落到分片，模型不需要自己判断该用哪种模式。
    """
    aid = (asset_id or "").strip()
    if not aid:
        return {"error": "asset_id is required"}
    try:
        start = max(0, int(offset or 0))
        span = int(limit or DEFAULT_LIMIT)
    except (TypeError, ValueError):
        return {"error": "offset/limit must be integers"}
    span = max(1, min(span, MAX_LIMIT))

    from app.media.attachments import _extract_body, _fetch_asset_bytes

    tenant_id = get_tenant_id_int()
    user_id = get_user_id()
    if not tenant_id or not user_id:
        # fail-closed：没有身份就不发请求。网关也会拒，但让模型看到"原因在归属"比
        # 看到一个 404 更有用 —— 后者容易被误读成"附件不存在"。
        return {"error": "attachment_read requires an authenticated tenant/user context"}

    asked = (query or "").strip()
    if asked:
        from app.knowledge.attachment_index import query_attachment

        try:
            hit = await query_attachment(
                tenant_id=tenant_id, user_id=user_id, asset_id=aid, question=asked
            )
        except Exception as exc:  # noqa: BLE001 — 索引故障不该让读附件失败
            logger.warning("attachment_read: indexed query failed asset=%s: %s", aid, exc)
            hit = None
        if hit is not None:
            return {"asset_id": aid, "mode": "index", "query": asked, **hit}

    got = await _fetch_asset_bytes(aid, tenant_id, user_id)
    if got is None:
        return {
            "error": (
                f"attachment not found or unavailable: {aid}"
                "（不存在、不属于当前用户，或取回通道不可达）"
            )
        }
    data, mime, name = got
    text = _extract_body(data, mime, name or aid)
    if not text.strip():
        return {
            "asset_id": aid,
            "name": name,
            "mime_type": mime,
            "error": (
                "this attachment has no extractable text"
                "（图片请用 vision_analyze，其它二进制请用 file_analyzer）"
            ),
        }

    total = len(text)
    chunk = text[start : start + span]
    end = start + len(chunk)
    result: dict[str, Any] = {
        "asset_id": aid,
        "name": name,
        "mime_type": mime,
        "offset": start,
        "returned_chars": len(chunk),
        "total_chars": total,
        "content": chunk,
    }
    if end < total:
        result["note"] = f"还有 {total - end} 字符未读；用 offset={end} 继续取。"
    else:
        result["note"] = "已读到结尾。"
    return result


registry.register(
    name="attachment_read",
    description=(
        "Read an attachment by its asset_id. Without `query` it returns text paged with "
        "offset/limit; with `query` and an indexed attachment (PDF) it answers the question "
        "against that document and returns page citations — prefer this for long PDFs. "
        "Attachments are referenced in history as <attachment id=... /> rather than carried "
        "inline, so use this to read any part you did not already see — do not ask the user "
        "to re-send the file. For images use vision_analyze instead."
    ),
    parameters={
        "type": "object",
        "properties": {
            "asset_id": {
                "type": "string",
                "description": "The `id` from an <attachment id=... /> reference",
            },
            "query": {
                "type": "string",
                "description": (
                    "Question to answer against an indexed document (PDF). "
                    "Omit to read raw text by offset/limit."
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
                "description": f"Max characters to return (max {MAX_LIMIT})",
            },
        },
        "required": ["asset_id"],
    },
    handler=attachment_read,
)
