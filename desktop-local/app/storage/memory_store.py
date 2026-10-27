"""本地记忆层（``local_memory`` 表）的 DDL 与读写。

最小闭环：条目是「标题 + 正文 + 可选标签/来源」，检索用 **ILIKE 关键词**。
契约 §4 没有规定检索算法，第二轮若要接全文/向量，替换 :func:`search_entries` 即可，
其余接口形状不用动。

几个实现取舍：

- ``tags`` 落库为**逗号分隔字符串**（``"pg,存储"``）：本批只做关键词匹配，不需要数组类型，
  也不需要 ``pgcrypto``；对外返回时拆成 ``list[str]``。
- ``id`` 由调用方给 ``uuid4().hex``，不依赖 ``gen_random_uuid()``（embedded PostgreSQL 不一定
  装了 ``pgcrypto``）。也正因如此 ``id`` 列是 ``TEXT`` 而不是 ``uuid``。
- 建表用 ``CREATE TABLE IF NOT EXISTS``，每次请求前跑一遍（本地单用户、语句幂等且便宜）。
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

import asyncpg

# 输入上限：与 API 层的 pydantic 约束共用，避免路由和 SQL 两边各写一套魔法数字
MAX_CONTENT_CHARS = 100_000
MAX_TITLE_CHARS = 500
MAX_KIND_CHARS = 64
MAX_SOURCE_CHARS = 500
MAX_TAGS = 64
MAX_KEYWORD_CHARS = 200
DEFAULT_KIND = "note"

MEMORY_COLUMNS = "id, kind, title, content, tags, source, created_at"

MEMORY_DDL = """
CREATE TABLE IF NOT EXISTS local_memory (
    id          TEXT PRIMARY KEY,
    kind        TEXT NOT NULL DEFAULT 'note',
    title       TEXT NOT NULL DEFAULT '',
    content     TEXT NOT NULL,
    tags        TEXT NOT NULL DEFAULT '',
    source      TEXT NOT NULL DEFAULT '',
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
)
"""


class MemoryStoreError(ValueError):
    """记忆层输入不合法（空关键词等）。``status_code`` 由路由层直接使用。"""

    status_code = 400


# --------------------------------------------------------------------- 标签与关键词
def normalize_tags(tags: Sequence[str] | str | None) -> str:
    """标签统一落成逗号分隔字符串：折叠空白、去掉逗号、去重、限个数。"""
    if tags is None:
        return ""
    items = [tags] if isinstance(tags, str) else list(tags)
    cleaned: list[str] = []
    for item in items:
        # 先把逗号换成空格再折叠空白：标签里出现逗号会破坏分隔语义
        value = " ".join(str(item).replace(",", " ").split())
        if value and value not in cleaned:
            cleaned.append(value)
    return ",".join(cleaned[:MAX_TAGS])


def split_tags(raw: Any) -> list[str]:
    """把落库的逗号分隔字符串还原成 ``list[str]``。"""
    if not raw:
        return []
    return [item for item in str(raw).split(",") if item]


def clean_keyword(query: str) -> str:
    """收拢检索关键词：折叠空白、限制长度；空则报错（不做全表扫描式的"空查询"）。"""
    keyword = " ".join((query or "").split())
    if not keyword:
        raise MemoryStoreError("query 不能为空白")
    if len(keyword) > MAX_KEYWORD_CHARS:
        raise MemoryStoreError(f"query 过长：{len(keyword)} > 上限 {MAX_KEYWORD_CHARS} 字符")
    return keyword


def like_pattern(keyword: str) -> str:
    """``%关键词%``，并转义 LIKE 元字符（``%`` ``_`` ``\\``）。"""
    escaped = keyword.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return f"%{escaped}%"


# --------------------------------------------------------------------- 数据库
async def connect(dsn: str) -> asyncpg.Connection:
    """建一条 asyncpg 连接（本地单用户，本批不做连接池）。"""
    return await asyncpg.connect(dsn=dsn)


async def ensure_memory_schema(conn: asyncpg.Connection) -> None:
    await conn.execute(MEMORY_DDL)


def row_to_dict(row: Any) -> dict[str, Any]:
    """asyncpg ``Record`` → ``dict``（``Record`` 迭出的是值，不能 ``dict(record)``）。"""
    return {key: row[key] for key in row.keys()}


async def insert_entry(
    conn: asyncpg.Connection,
    *,
    entry_id: str,
    kind: str,
    title: str,
    content: str,
    tags: str,
    source: str,
) -> dict[str, Any]:
    row = await conn.fetchrow(
        f"INSERT INTO local_memory ({MEMORY_COLUMNS}) "
        "VALUES ($1, $2, $3, $4, $5, $6) "
        f"RETURNING {MEMORY_COLUMNS}",
        entry_id,
        kind,
        title,
        content,
        tags,
        source,
    )
    if row is None:
        raise RuntimeError("INSERT INTO local_memory ... RETURNING 没有返回行")
    return row_to_dict(row)


async def list_entries(conn: asyncpg.Connection, *, limit: int, offset: int) -> list[dict[str, Any]]:
    """按写入时间倒序分页。本批不做 ``kind`` 过滤（最小闭环，见模块文档）。"""
    rows = await conn.fetch(
        f"SELECT {MEMORY_COLUMNS} FROM local_memory "
        "ORDER BY created_at DESC, id DESC LIMIT $1 OFFSET $2",
        limit,
        offset,
    )
    return [row_to_dict(row) for row in rows]


async def delete_entry(conn: asyncpg.Connection, entry_id: str) -> bool:
    row = await conn.fetchrow("DELETE FROM local_memory WHERE id = $1 RETURNING id", entry_id)
    return row is not None


async def search_entries(conn: asyncpg.Connection, keyword: str, *, limit: int) -> list[dict[str, Any]]:
    """标题 / 正文 / 标签任一命中即可（大小写不敏感的子串匹配）。"""
    rows = await conn.fetch(
        f"SELECT {MEMORY_COLUMNS} FROM local_memory "
        "WHERE title ILIKE $1 OR content ILIKE $1 OR tags ILIKE $1 "
        "ORDER BY created_at DESC, id DESC LIMIT $2",
        like_pattern(keyword),
        limit,
    )
    return [row_to_dict(row) for row in rows]


# --------------------------------------------------------------------- 对外形状
def serialize_entry(row: dict[str, Any]) -> dict[str, Any]:
    created_at = row.get("created_at")
    return {
        "id": str(row["id"]),
        "kind": str(row["kind"]),
        "title": str(row["title"]),
        "content": str(row["content"]),
        "tags": split_tags(row.get("tags")),
        "source": str(row["source"]),
        "created_at": created_at.isoformat() if hasattr(created_at, "isoformat") else str(created_at or ""),
    }
