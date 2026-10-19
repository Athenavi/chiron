"""PageIndex 回答的归一化与**越界引用守卫**。

两个 provider 共用一份，理由很直接：这两件事一旦各写一份，就会各自漂移 —— 而其中一件是
安全相关（越界判定）。

## 为什么要"拒绝返回"而不是"过滤掉越界引用"

PageIndex 的 ``doc_id`` / ``folder_id`` 官方语义是**引导**（"steer discovery"），
不是硬限制。也就是说，模型有可能读到本次范围之外的文档 —— 表现就是引用的文档名不属于
本库。此时：

* **只删引用**：答案正文里可能已经写进了范围外内容，删掉引用反而把"答案含别处的内容"
  伪装成"一条没有出处的普通回答"，人看不出问题；
* **整条拒绝**：用户看到"检索范围异常"，运维在日志里看到越界文档名 —— 信息不泄露，
  问题可观测。

代价是可能的误杀（PageIndex 改写文档名时解析不出前缀）。所以 ``citation_in_scope``
在**解析不出 doc_id 前缀时判为通过**，只有"能解析出、且不属于本库"才拒绝。
"""

from __future__ import annotations

import logging
from collections.abc import Iterable
from typing import Any

from app.knowledge.errors import ProviderFailed
from app.knowledge.providers.base import (
    AnswerResult,
    Citation,
    citation_in_scope,
)

logger = logging.getLogger(__name__)


def normalize_chat_result(raw: Any) -> AnswerResult:
    """把 ``chat(citations=True)`` 的返回归一化。

    SDK 的返回随模式而异（裸字符串、dict、或流式迭代器）：这里只认"非流式"的两种形态，
    遇到别的形态就退化成"把文本当答案、无引用"，而不是抛错丢掉整段回答。
    """
    if isinstance(raw, str):
        return AnswerResult(answer=raw)
    if isinstance(raw, dict):
        answer = str(raw.get("answer") or raw.get("content") or raw.get("text") or "")
        citations = [
            Citation(
                doc_name=str(item.get("doc_name") or item.get("name") or ""),
                page=_as_int(item.get("page") or item.get("page_num")),
                block_id=str(item.get("block_id") or ""),
                snippet=str(item.get("snippet") or item.get("text") or "")[:500],
            )
            for item in (raw.get("citations") or [])
            if isinstance(item, dict)
        ]
        return AnswerResult(answer=answer, citations=citations)
    return AnswerResult(answer=str(raw))


def guard_citations(
    result: AnswerResult, doc_ids: Iterable[str], *, provider: str
) -> AnswerResult:
    """越界引用 → 整条拒绝（见模块 docstring）。"""
    allowed = {d for d in doc_ids if d}
    strays = [
        c.doc_name
        for c in result.citations
        if c.doc_name and not citation_in_scope(c.doc_name, allowed)
    ]
    if strays:
        logger.warning(
            "knowledge: %s returned out-of-scope citations: %s", provider, strays[:3]
        )
        raise ProviderFailed(
            "检索结果包含知识库范围外的文档，已拒绝返回"
            "（请检查 provider 侧的文档归属）"
        )
    return result


def _as_int(value: Any) -> int | None:
    try:
        return int(value) if value is not None else None
    except (TypeError, ValueError):
        return None
