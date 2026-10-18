"""L3 写入侧 store（`app/memory/summaries.py`）的向量落库。

生产里 Consolidator 用的就是这个 store（`app/main.py` 的四层装配），所以"摘要向量有没有
数据来源"这件事必须由这里来证明，而不是只测读取侧的 `save_summary`。对应
`docs/core-mechanisms-paper.md` §5.1 与迁移 0019。
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest

from app.config import settings
from app.memory.layers import SummaryEntry
from app.memory.summaries import SummaryStore

#: INSERT 里按位置绑定的 11 个参数（其余 4 个是写死的 0 / NULL / 'active' / NOW()）。
_INSERT_FIELDS = (
    "id",
    "tenant_id",
    "user_id",
    "session_id",
    "content",
    "topics",
    "entities",
    "turn_start",
    "turn_end",
    "content_hash",
    "embedding",
)


class _RecordingPool:
    """按「投影决定可见列」的方式回放的 asyncpg 替身。

    这个口径是故意的：真实 SQL 不会返回没 SELECT 的列，替身要是无条件把 `embedding`
    塞进行里，`list_active(with_embedding=False)` 少取一列这件事就永远测不出来。
    """

    def __init__(self) -> None:
        self.rows: dict[str, dict[str, Any]] = {}
        self.selects: list[str] = []

    async def fetchrow(self, query: str, *args) -> dict | None:
        if "INSERT INTO memory_summaries" in query:
            row = dict(zip(_INSERT_FIELDS, args, strict=True))
            row.update(
                access_count=0,
                last_accessed_at=None,
                status="active",
                created_at=datetime.now(UTC),
            )
            self.rows[row["id"]] = row
            return {"id": row["id"]}
        if "WHERE id=$1" in query:
            sid, tenant_id, user_id = args
            row = self.rows.get(sid)
            if row and row["tenant_id"] == tenant_id and row["user_id"] == user_id:
                return self._project(query, row)
            return None
        if "WHERE content_hash=$1" in query:
            ch, tenant_id, user_id = args
            for row in self.rows.values():
                if (
                    row["content_hash"] == ch
                    and row["tenant_id"] == tenant_id
                    and row["user_id"] == user_id
                ):
                    return self._project(query, row)
            return None
        return None

    async def fetch(self, query: str, *args) -> list[dict]:
        if "FROM memory_summaries" in query:
            self.selects.append(query)
            tenant_id, user_id, limit = args[0], args[1], args[2]
            matched = [
                self._project(query, r)
                for r in self.rows.values()
                if r["tenant_id"] == tenant_id
                and r["user_id"] == user_id
                and r["status"] == "active"
            ]
            return matched[:limit]
        return []

    async def execute(self, query: str, *args) -> str:
        return "UPDATE 1"

    @staticmethod
    def _project(query: str, row: dict[str, Any]) -> dict[str, Any]:
        if "embedding" not in query:
            return {k: v for k, v in row.items() if k != "embedding"}
        return dict(row)


class _MilvusSpy:
    def __init__(self) -> None:
        self.inserted: list[list] = []

    def insert(self, data: list) -> None:
        self.inserted.append(data)

    def flush(self, **kwargs) -> None:
        pass


def _entry(content: str = "讨论 Go 架构", **kw: Any) -> SummaryEntry:
    return SummaryEntry(
        id=kw.pop("id", "sms_test0000000000000"),
        tenant_id=kw.pop("tenant_id", "t1"),
        user_id=kw.pop("user_id", "u1"),
        session_id=kw.pop("session_id", "s1"),
        content=content,
        topics=["go", "架构"],
        entities={},
        turn_start=0,
        turn_end=2,
        content_hash=kw.pop("content_hash", ""),
        **kw,
    )


@pytest.fixture
def pool() -> _RecordingPool:
    return _RecordingPool()


@pytest.fixture
def milvus() -> _MilvusSpy:
    return _MilvusSpy()


VEC = [1.0, 0.0, 0.0]


class TestVectorLandsInPg:
    """默认后端（pg）：向量是这一行的一个列，不是外部库里的副本。"""

    async def test_insert_binds_vector_to_the_embedding_column(self, pool, milvus):
        await SummaryStore(pool, milvus).insert(_entry(), embedding=VEC)

        stored = next(iter(pool.rows.values()))
        assert stored["embedding"] == VEC

    async def test_default_backend_does_not_mirror_to_milvus(self, pool, milvus):
        """配成 pg 时没人查 Milvus —— 写它只是多一次外部调用和一个故障面。"""
        store = SummaryStore(pool, milvus)
        assert settings.memory_vector_backend == "pg", "默认值变了这条用例就不再守住"

        await store.insert(_entry(), embedding=VEC)

        assert milvus.inserted == []

    async def test_milvus_backend_mirrors_with_the_callers_vector(self, pool, milvus, monkeypatch):
        monkeypatch.setattr(settings, "memory_vector_backend", "milvus")

        await SummaryStore(pool, milvus).insert(_entry(), embedding=VEC)

        assert len(milvus.inserted) == 1
        # data 是按字段分列的列表：[ids], [tenant], [user], [session], [content], [type], [vector], ...
        assert milvus.inserted[0][6] == [VEC]
        # 镜像只是副本：PG 那份必须同时在（切回 pg 才不至于没有数据）
        assert next(iter(pool.rows.values()))["embedding"] == VEC

    async def test_row_without_vector_still_inserts(self, pool, milvus):
        """没有嵌入（embedder 挂了）不挡正文：这条之后仍可按时间被召回。"""
        created = await SummaryStore(pool, milvus).insert(_entry(), embedding=None)

        assert next(iter(pool.rows.values()))["embedding"] is None
        assert created.embedding is None

    async def test_created_entry_carries_the_vector_without_an_extra_read(self, pool, milvus):
        """`get_by_id` 的投影不含向量，所以返回体由手上那份直接挂上（省一次 6KB 往返）。"""
        created = await SummaryStore(pool, milvus).insert(_entry(), embedding=VEC)

        assert created.embedding == VEC


class TestProjection:
    """向量只在真要算余弦的那条路上取（M12 定下的口径，这里对写入侧 store 同样成立）。"""

    async def test_list_active_omits_embedding_by_default(self, pool, milvus):
        await SummaryStore(pool, milvus).insert(_entry(), embedding=VEC)

        items = await SummaryStore(pool, milvus).list_active("t1", "u1", 50)

        assert "embedding" not in pool.selects[-1]
        assert items[0].embedding is None

    async def test_list_active_with_embedding_reads_it_back(self, pool, milvus):
        await SummaryStore(pool, milvus).insert(_entry(), embedding=VEC)

        items = await SummaryStore(pool, milvus).list_active(
            "t1", "u1", 50, with_embedding=True
        )

        assert "embedding" in pool.selects[-1]
        assert items[0].embedding == VEC

    async def test_get_by_id_projection_has_no_vector(self, pool, milvus):
        """单条读取走的是不含向量的投影 —— 判重扫描不该用它，别把它误当"这条没有向量"。"""
        await SummaryStore(pool, milvus).insert(_entry(), embedding=VEC)

        row = await SummaryStore(pool, milvus).get_by_id("t1", "u1", "sms_test0000000000000")

        assert row.embedding is None
