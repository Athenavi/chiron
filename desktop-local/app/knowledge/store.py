"""本地知识库的 PG 存储层（表 + 原文落盘）。

三张表（``CREATE TABLE IF NOT EXISTS``，不依赖迁移 — 本批不改 ``migrations/``）：

- ``local_kb``        知识库
- ``local_kb_docs``   文档元数据（原文不放库，落 ``<data-dir>/knowledge/<kb>/<doc>``）
- ``local_kb_chunks`` 分块内容（检索的单位）

原文落盘而不是进 PG：契约 §4 的数据目录就是用户可见的"我的文件"，用户能直接看/备份；
PG 只存元数据与分块，库坏了也能从原文重建。

连接：每次操作 ``asyncpg.connect(dsn=...)`` 即连即断。本地单用户、QPS 极低，
连接池带来的复杂度不值得；真需要池化时改这里一个地方就够。
"""

from __future__ import annotations

import asyncio
import re
import shutil
import uuid
from collections.abc import AsyncIterator, Mapping, Sequence
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

SCHEMA_SQL: tuple[str, ...] = (
    """
    CREATE TABLE IF NOT EXISTS local_kb (
        id          TEXT PRIMARY KEY,
        name        TEXT NOT NULL,
        description TEXT NOT NULL DEFAULT '',
        created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS local_kb_docs (
        id         TEXT PRIMARY KEY,
        kb_id      TEXT NOT NULL REFERENCES local_kb(id) ON DELETE CASCADE,
        name       TEXT NOT NULL,
        path       TEXT NOT NULL,
        size_bytes INTEGER NOT NULL DEFAULT 0,
        mime_type  TEXT NOT NULL DEFAULT 'text/plain',
        created_at TIMESTAMPTZ NOT NULL DEFAULT now()
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS local_kb_chunks (
        id         TEXT PRIMARY KEY,
        kb_id      TEXT NOT NULL REFERENCES local_kb(id) ON DELETE CASCADE,
        doc_id     TEXT NOT NULL REFERENCES local_kb_docs(id) ON DELETE CASCADE,
        seq        INTEGER NOT NULL,
        content    TEXT NOT NULL,
        created_at TIMESTAMPTZ NOT NULL DEFAULT now()
    )
    """,
    "CREATE INDEX IF NOT EXISTS local_kb_docs_kb_idx ON local_kb_docs (kb_id)",
    "CREATE INDEX IF NOT EXISTS local_kb_chunks_kb_idx ON local_kb_chunks (kb_id, doc_id, seq)",
)

# id / 目录名都只允许这套字符（服务端生成的就是 doc_/kb_/chk_ + hex），
# 这样 ``<data-dir>/knowledge/<kb_id>`` 不可能被请求参数带出目录边界。
_ID_SEGMENT = re.compile(r"[A-Za-z0-9_-]{1,64}")
_SAFE_SUFFIX = re.compile(r"\.[A-Za-z0-9]{1,8}")


class StoreUnavailable(RuntimeError):  # noqa: N818 —— 与 storage/pg.py 的 PostgresUnavailable 命名保持一致
    """PG 不可用（DSN 为空 / asyncpg 缺失 / 连不上）。API 层映射成 503。"""


def new_id(prefix: str) -> str:
    """生成带前缀的短 id（``kb_1a2b...``），可读且天然有序于日志里。"""
    return f"{prefix}_{uuid.uuid4().hex[:16]}"


def safe_segment(value: str, what: str = "id") -> str:
    """校验一个会被拼进路径的片段；不合法抛 ``ValueError``。"""
    text = str(value or "")
    if not _ID_SEGMENT.fullmatch(text):
        raise ValueError(f"{what} 只允许 [A-Za-z0-9_-]{{1,64}}：{text!r}")
    return text


def document_path(root: Path, kb_id: str, doc_id: str, filename: str) -> Path:
    """原文落盘位置：``<root>/<kb>/<doc_id><suffix>``。

    用 ``doc_id`` 当文件名（后缀沿用上传名的安全后缀），不直接用用户文件名：
    同名覆盖、以及 ``..`` / 盘符 / 分隔符这类路径注入就都不存在了；原始文件名留在
    ``local_kb_docs.name`` 里，展示时读那张表。
    """
    suffix = ""
    candidate = Path(str(filename or "")).suffix
    if candidate and _SAFE_SUFFIX.fullmatch(candidate):
        suffix = candidate
    return root / safe_segment(kb_id, "kb_id") / f"{safe_segment(doc_id, 'doc_id')}{suffix}"


def like_pattern(term: str) -> str:
    """ILIKE 参数：转义 ``\\`` ``%`` ``_``，两端加 ``%``（配合 SQL 里的 ``ESCAPE E'\\\\'``）。"""
    escaped = str(term or "").replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return f"%{escaped}%"


def search_sql(terms: Sequence[str], limit: int) -> tuple[str, list[Any]]:
    """拼 ``local_kb_chunks`` 的检索 SQL：``kb_id`` 固定 ``$1``，关键词从 ``$2`` 起。

    抽成纯函数是为了能单测 —— ``$n`` 与参数个数必须严格对齐，拼错了 asyncpg 会报
    "the server expects N arguments"。

    转义用 ``ESCAPE E'\\\\'`` 而不是 ``ESCAPE '\\'``：E-string 里 ``\\\\`` 恒定表示一个
    反斜杠，不依赖数据库的 ``standard_conforming_strings`` 设置，正好对上
    :func:`like_pattern` 的转义方式。
    """
    cleaned = [term for term in terms if term]
    params: list[Any] = []
    if cleaned:
        clauses = " OR ".join(f"content ILIKE ${index + 2} ESCAPE E'\\\\'" for index in range(len(cleaned)))
        sql = (
            "SELECT id, doc_id, seq, content FROM local_kb_chunks "
            f"WHERE kb_id = $1 AND ({clauses}) ORDER BY doc_id, seq LIMIT ${len(cleaned) + 2}"
        )
        params.extend(like_pattern(term) for term in cleaned)
    else:
        sql = (
            "SELECT id, doc_id, seq, content FROM local_kb_chunks "
            "WHERE kb_id = $1 ORDER BY doc_id, seq LIMIT $2"
        )
    params.append(max(1, int(limit)))
    return sql, params


def _write_bytes(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)


def _iso(value: Any) -> str:
    if value is None:
        return ""
    isoformat = getattr(value, "isoformat", None)
    return str(isoformat()) if callable(isoformat) else str(value)


def kb_json(row: Mapping[str, Any]) -> dict[str, Any]:
    """``local_kb`` 行 → API 契约里的知识库对象。"""
    return {
        "id": str(row["id"]),
        "name": str(row["name"]),
        "description": str(row.get("description") or ""),
        "created_at": _iso(row.get("created_at")),
    }


def doc_json(row: Mapping[str, Any], chunk_count: int = 0) -> dict[str, Any]:
    """``local_kb_docs`` 行 → API 契约里的文档对象。"""
    return {
        "doc_id": str(row["id"]),
        "kb_id": str(row["kb_id"]),
        "name": str(row["name"]),
        "path": str(row["path"]),
        "size_bytes": int(row.get("size_bytes") or 0),
        "mime_type": str(row.get("mime_type") or "text/plain"),
        "chunks": int(chunk_count),
        "created_at": _iso(row.get("created_at")),
    }


class PgKnowledgeStore:
    """``local_kb*`` 三张表 + 原文目录的读写。

    所有方法都是协程；任何"连不上 PG"都归一成 :class:`StoreUnavailable`，
    免得 API 层要区分 asyncpg 的十几种异常。
    """

    def __init__(self, dsn: str, root: Path, timeout: float = 10.0) -> None:
        self.dsn = dsn or ""
        self.root = Path(root)
        self.timeout = timeout
        self._schema_ready = False

    # ------------------------------------------------------------------ 连接
    @asynccontextmanager
    async def connect(self) -> AsyncIterator[Any]:
        """开一条连接；首次使用时建表（幂等 DDL）。"""
        if not self.dsn:
            raise StoreUnavailable("PG 未启动（request.app.state.pg.dsn 为空），本地知识库不可用")
        try:
            import asyncpg
        except ImportError as exc:  # pragma: no cover —— 主依赖里有 asyncpg
            raise StoreUnavailable(f"asyncpg 不可用：{exc}") from exc

        try:
            conn = await asyncpg.connect(dsn=self.dsn, timeout=self.timeout)
        except Exception as exc:  # noqa: BLE001 —— 连不上就是要报"不可用"
            raise StoreUnavailable(f"连接 PG 失败：{type(exc).__name__}: {exc}") from exc

        try:
            if not self._schema_ready:
                for statement in SCHEMA_SQL:
                    await conn.execute(statement)
                self._schema_ready = True
            yield conn
        finally:
            await conn.close()

    # -------------------------------------------------------------- 知识库 CRUD
    async def create_kb(self, name: str, description: str = "") -> dict[str, Any]:
        kb_id = new_id("kb")
        async with self.connect() as conn:
            row = await conn.fetchrow(
                "INSERT INTO local_kb (id, name, description) VALUES ($1, $2, $3) "
                "RETURNING id, name, description, created_at",
                kb_id,
                name,
                description,
            )
        await asyncio.to_thread((self.root / kb_id).mkdir, parents=True, exist_ok=True)
        return kb_json(dict(row))

    async def list_kbs(self) -> list[dict[str, Any]]:
        async with self.connect() as conn:
            rows = await conn.fetch(
                "SELECT id, name, description, created_at FROM local_kb ORDER BY created_at DESC, id DESC"
            )
        return [kb_json(dict(row)) for row in rows]

    async def get_kb(self, kb_id: str) -> dict[str, Any] | None:
        key = safe_segment(kb_id, "kb_id")
        async with self.connect() as conn:
            row = await conn.fetchrow(
                "SELECT id, name, description, created_at FROM local_kb WHERE id = $1", key
            )
        return kb_json(dict(row)) if row is not None else None

    async def delete_kb(self, kb_id: str) -> bool:
        """删库行（docs / chunks 由外键级联带走）+ 删原文目录。"""
        key = safe_segment(kb_id, "kb_id")
        async with self.connect() as conn:
            row = await conn.fetchrow("DELETE FROM local_kb WHERE id = $1 RETURNING id", key)
        await asyncio.to_thread(shutil.rmtree, self.root / key, True)
        return row is not None

    # ---------------------------------------------------------------- 文档与分块
    async def add_document(
        self,
        *,
        kb_id: str,
        name: str,
        data: bytes,
        mime_type: str,
        chunks: Sequence[str],
    ) -> dict[str, Any]:
        """写原文 + 元数据 + 分块（同一个事务里的两次 INSERT）。"""
        key = safe_segment(kb_id, "kb_id")
        doc_id = new_id("doc")
        path = document_path(self.root, key, doc_id, name)
        await asyncio.to_thread(_write_bytes, path, data)
        relative = path.relative_to(self.root).as_posix()

        async with self.connect() as conn, conn.transaction():
            row = await conn.fetchrow(
                "INSERT INTO local_kb_docs (id, kb_id, name, path, size_bytes, mime_type) "
                "VALUES ($1, $2, $3, $4, $5, $6) "
                "RETURNING id, kb_id, name, path, size_bytes, mime_type, created_at",
                doc_id,
                key,
                name,
                relative,
                len(data),
                mime_type,
            )
            await conn.executemany(
                "INSERT INTO local_kb_chunks (id, kb_id, doc_id, seq, content) VALUES ($1, $2, $3, $4, $5)",
                [(new_id("chk"), key, doc_id, index, body) for index, body in enumerate(chunks)],
            )
        return doc_json(dict(row), chunk_count=len(chunks))

    # ------------------------------------------------------------------ 检索
    async def search(self, *, kb_id: str, terms: Sequence[str], limit: int = 200) -> list[dict[str, Any]]:
        """按关键词取回候选片段（ILIKE OR）。排序留给 :mod:`app.knowledge.search`。"""
        key = safe_segment(kb_id, "kb_id")
        sql, extra = search_sql(terms, limit)

        async with self.connect() as conn:
            rows = await conn.fetch(sql, key, *extra)
        return [
            {
                "id": str(row["id"]),
                "doc_id": str(row["doc_id"]),
                "seq": int(row["seq"]),
                "content": str(row["content"]),
            }
            for row in rows
        ]
