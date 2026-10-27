"""本地知识库（极简闭环）。

模块分工：

- :mod:`app.knowledge.chunk`    文本分块（800 字 / 重叠 100）
- :mod:`app.knowledge.store`    ``local_kb*`` 三张表 + 原文落盘
- :mod:`app.knowledge.search`   关键词检索与答案拼装（不调 LLM）
- :mod:`app.knowledge.pageindex` PageIndex 组件探测（只探测，不启动）

**默认禁用**：``config.py`` 不在本批可改范围内，所以开关走环境变量
``CHIRON_LOCAL_KNOWLEDGE_ENABLED``（``1/true/yes/on`` 才算开）。关着的时候所有
``/v1/kb*`` 端点统一返回 403 ``{"error": "knowledge_disabled"}`` —— 明确报错，
不是静默 200 空列表。
"""

from __future__ import annotations

import os
from typing import TYPE_CHECKING, Any

from app.knowledge.chunk import DEFAULT_CHUNK_SIZE, DEFAULT_OVERLAP, chunk_text, normalize_text
from app.knowledge.pageindex import component_candidates, find_pageindex_dir, pageindex_status
from app.knowledge.search import build_answer, clip, keywords, rank_chunks, search_payload
from app.knowledge.store import (
    PgKnowledgeStore,
    StoreUnavailable,
    doc_json,
    document_path,
    kb_json,
    like_pattern,
    new_id,
    safe_segment,
    search_sql,
)

if TYPE_CHECKING:  # pragma: no cover —— 只为类型检查引入，运行期不需要
    from app.config import Settings

ENV_FLAG = "CHIRON_LOCAL_KNOWLEDGE_ENABLED"
_TRUTHY = frozenset({"1", "true", "yes", "on"})

__all__ = [
    "DEFAULT_CHUNK_SIZE",
    "DEFAULT_OVERLAP",
    "ENV_FLAG",
    "PgKnowledgeStore",
    "StoreUnavailable",
    "build_answer",
    "chunk_text",
    "clip",
    "component_candidates",
    "describe",
    "doc_json",
    "document_path",
    "find_pageindex_dir",
    "is_enabled",
    "kb_json",
    "keywords",
    "like_pattern",
    "new_id",
    "normalize_text",
    "pageindex_status",
    "rank_chunks",
    "safe_segment",
    "search_payload",
    "search_sql",
]


def is_enabled(settings: Settings | None = None) -> bool:
    """知识库是否开启。

    环境变量优先（便于测试与用户自选开启），其次看 ``settings`` 上有没有同名属性
    —— 以后 ``config.py`` 加上字段时这里无需改动。
    """
    raw = os.environ.get(ENV_FLAG)
    if raw is not None and raw.strip():
        return raw.strip().lower() in _TRUTHY
    return bool(getattr(settings, "knowledge_enabled", False))


def describe(settings: Settings) -> dict[str, Any]:
    """给 ``/readyz`` 之外的自检用的开关快照（不含路径之外的信息）。"""
    return {
        "enabled": is_enabled(settings),
        "env": ENV_FLAG,
        "chunk_size": DEFAULT_CHUNK_SIZE,
        "overlap": DEFAULT_OVERLAP,
    }
