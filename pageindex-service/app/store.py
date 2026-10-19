"""索引 worker 的数据库访问。

**只碰它需要的那几列**（worker 与引擎共享同一份 schema，但不共享代码 —— 两边的依赖树
无法共存）。因此这里对列名的假设必须与迁移保持一致；契约由"引擎侧写、worker 读"的
端到端用例兜住（``tests/test_index_contract.py``）。

写操作只有四类：文档状态、库状态、库的 index_ref / index_engine、用户的 Cloud folder。
业务状态机（谁能改、改成什么）仍以引擎的 ``app/knowledge/service.py`` 为准，这里只做
"把结果落下去"。
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)


class Store:
    def __init__(self, dsn: str) -> None:
        self._dsn = dsn
        self._pool: Any = None

    async def connect(self) -> None:
        import asyncpg

        self._pool = await asyncpg.create_pool(self._dsn, min_size=1, max_size=4)

    async def close(self) -> None:
        if self._pool is not None:
            await self._pool.close()
            self._pool = None

    # ── 读 ──

    async def get_kb(self, kb_id: str) -> Any:
        return await self._pool.fetchrow(
            """SELECT id, tenant_id, user_id, provider, index_ref, status, index_engine
                 FROM knowledge_bases WHERE id = $1""",
            kb_id,
        )

    async def get_user_settings(self, user_id: str) -> Any:
        return await self._pool.fetchrow(
            """SELECT provider, cloud_api_key_enc, cloud_folder_id
                 FROM user_knowledge_settings WHERE user_id = $1""",
            user_id,
        )

    async def pending_docs(self, kb_id: str, *, force: bool = False) -> list[Any]:
        """待索引文档。

        默认增量（``status <> 'ready'``）；``force=True`` 返回**全部**文档 —— 这是
        "升级 pageindex 后重建索引"的入口。没有 force 时，已 ready 的文档永远不会被
        重索引，升级 checklist 里的"重建"就无法落地。
        """
        where = (
            "knowledge_base_id = $1"
            if force
            else "knowledge_base_id = $1 AND status <> 'ready'"
        )
        return list(
            await self._pool.fetch(
                f"""SELECT id, name, file_type, storage_key
                      FROM knowledge_documents
                     WHERE {where}
                     ORDER BY created_at""",
                kb_id,
            )
        )

    # ── 写 ──

    async def set_kb_status(self, kb_id: str, status: str, error_message: str = "") -> None:
        await self._pool.execute(
            """UPDATE knowledge_bases
                  SET status = $2, error_message = $3, updated_at = NOW()
                WHERE id = $1""",
            kb_id,
            status,
            error_message or None,
        )

    async def set_kb_index_ref(self, kb_id: str, index_ref: str) -> None:
        await self._pool.execute(
            """UPDATE knowledge_bases
                  SET index_ref = $2, updated_at = NOW()
                WHERE id = $1 AND COALESCE(index_ref, '') <> $2""",
            kb_id,
            index_ref,
        )

    async def set_kb_index_engine(self, kb_id: str, engine: str) -> None:
        """记录建立/刷新索引时的 SDK 版本（升级后据此判断要不要重建）。"""
        await self._pool.execute(
            "UPDATE knowledge_bases SET index_engine = $2 WHERE id = $1",
            kb_id,
            engine,
        )

    async def set_doc_indexing(self, doc_id: str) -> None:
        await self._pool.execute(
            "UPDATE knowledge_documents SET status = 'indexing', updated_at = NOW() WHERE id = $1",
            doc_id,
        )

    async def set_doc_ready(
        self, doc_id: str, pageindex_doc_id: str, page_count: int
    ) -> None:
        await self._pool.execute(
            """UPDATE knowledge_documents
                  SET status = 'ready', pageindex_doc_id = $2, page_count = $3,
                      error_message = NULL, updated_at = NOW()
                WHERE id = $1""",
            doc_id,
            pageindex_doc_id,
            page_count,
        )

    async def set_doc_error(self, doc_id: str, message: str) -> None:
        await self._pool.execute(
            """UPDATE knowledge_documents
                  SET status = 'error', error_message = $2, updated_at = NOW()
                WHERE id = $1""",
            doc_id,
            message[:500],
        )

    async def set_cloud_folder(self, user_id: str, folder_id: str) -> None:
        await self._pool.execute(
            """UPDATE user_knowledge_settings
                  SET cloud_folder_id = $2, updated_at = NOW()
                WHERE user_id = $1""",
            user_id,
            folder_id,
        )
