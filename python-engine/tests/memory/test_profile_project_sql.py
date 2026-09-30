"""C3：L2 存储层的项目隔离 —— SQL 形态回归。

隔离不能只写在文档里。这里用**记录 SQL 的假 pool** 钉住：过滤、冲突目标、清空范围都必须
真的带 `project` —— 漏掉任何一处，就是"两个项目互相可见"或"同名键互相覆盖"。后者尤其隐蔽：
`ON CONFLICT` 少了 `project` 时 INSERT 不会报错，只会静默改写另一个项目的记忆。
"""

from __future__ import annotations

from typing import Any

from app.memory.layers import MemoryEntry
from app.memory.profile import _COLUMNS, ProfileStore


def _row(project: str = "proj-a") -> dict[str, Any]:
    """一行的完整形态（`_row_to_entry` 要求所有列都在）。"""
    return {
        "id": "mem_1",
        "tenant_id": "t1",
        "user_id": "u1",
        "slot": "preference",
        "item_key": "lang",
        "item_value": "中文",
        "confidence": 60,
        "source": "user_confirmed",
        "embedding": None,
        "access_count": 0,
        "last_accessed_at": None,
        "status": "active",
        "created_at": None,
        "updated_at": None,
        "project": project,
    }


class _RecordingPool:
    """记录 SQL 与参数（本文件只验证"发出去的语句"，不模拟行）。"""

    def __init__(self) -> None:
        self.calls: list[tuple[str, tuple[Any, ...]]] = []

    async def fetch(self, sql: str, *params: Any) -> list[Any]:
        self.calls.append((sql, params))
        return []

    async def fetchrow(self, sql: str, *params: Any) -> dict[str, Any]:
        self.calls.append((sql, params))
        return _row()

    async def fetchval(self, sql: str, *params: Any) -> int:
        self.calls.append((sql, params))
        return 0

    async def execute(self, sql: str, *params: Any) -> None:
        self.calls.append((sql, params))
        return None


def _norm(sql: str) -> str:
    return " ".join(sql.split())


def _entry(project: str = "proj-a") -> MemoryEntry:
    return MemoryEntry(
        id="mem_1",
        tenant_id="t1",
        user_id="u1",
        slot="preference",
        item_key="lang",
        item_value="中文",
        confidence=60,
        source="user_confirmed",
        project=project,
    )


def _store() -> tuple[ProfileStore, _RecordingPool]:
    pool = _RecordingPool()
    return ProfileStore(pool), pool


# ── SELECT 列表 ─────────────────────────────────────────────────────────


def test_select_columns_include_project():
    """列清单漏了 project ⇒ 读回的对象永远显示"未分组"。"""
    assert "project" in _COLUMNS


# ── 读 ──────────────────────────────────────────────────────────────────


async def test_list_filters_by_project():
    store, pool = _store()

    await store.list("t1", "u1", project="proj-a")

    sql, params = pool.calls[0]
    assert "project=$3" in _norm(sql)
    assert params[:3] == ("t1", "u1", "proj-a")


async def test_list_defaults_to_ungrouped():
    store, pool = _store()

    await store.list("t1", "u1")

    assert pool.calls[0][1][2] == "", "缺省 = 未分组（空串），而不是'不过滤'"


async def test_get_by_key_and_count_filter_by_project():
    store, pool = _store()

    await store.get_by_key("t1", "u1", "preference", "lang", project="proj-a")
    await store.count("t1", "u1", project="proj-a")

    get_sql, get_params = pool.calls[0]
    count_sql, count_params = pool.calls[1]
    assert "item_key=$4 AND project=$5" in _norm(get_sql)
    assert get_params[-1] == "proj-a"
    assert "project=$3" in _norm(count_sql) and count_params[-1] == "proj-a"


# ── 写 ──────────────────────────────────────────────────────────────────


async def test_insert_conflict_target_includes_project():
    """最隐蔽的一处：`ON CONFLICT` 少了 project 会**静默改写**另一个项目的记忆。"""
    store, pool = _store()

    await store.insert(_entry("proj-a"))

    sql, params = _norm(pool.calls[0][0]), pool.calls[0][1]
    assert "ON CONFLICT (tenant_id, user_id, project, slot, item_key)" in sql
    assert params[-1] == "proj-a", "写入值也要落到 project 列"


async def test_update_and_delete_filter_by_project():
    store, pool = _store()

    await store.update("t1", "u1", "mem_1", item_value="新值", project="proj-a")
    await store.delete("t1", "u1", "mem_1", project="proj-a")

    update_sql, update_params = _norm(pool.calls[0][0]), pool.calls[0][1]
    delete_sql, delete_params = _norm(pool.calls[1][0]), pool.calls[1][1]
    assert "id=$3 AND project=$4" in update_sql, "按 id 改也要限定项目"
    assert update_params[3] == "proj-a"
    assert "id=$3 AND project=$4" in delete_sql and delete_params[-1] == "proj-a"


async def test_delete_by_key_and_delete_all_filter_by_project():
    store, pool = _store()

    await store.delete_by_key("t1", "u1", "lang", project="proj-a")
    await store.delete_all("t1", "u1", project="proj-a")

    by_key_sql, by_key_params = _norm(pool.calls[0][0]), pool.calls[0][1]
    all_sql, all_params = _norm(pool.calls[1][0]), pool.calls[1][1]
    assert "item_key=$3 AND project=$4" in by_key_sql
    assert by_key_params[-1] == "proj-a"
    assert "project=$3" in all_sql and all_params[-1] == "proj-a"


# ── 行模型 ──────────────────────────────────────────────────────────────


def test_memory_entry_defaults_to_ungrouped():
    entry = MemoryEntry(
        id="m",
        tenant_id="t",
        user_id="u",
        slot="fact",
        item_key="k",
        item_value="v",
        confidence=50,
        source="derived",
    )

    assert entry.project == ""
    assert entry.to_dict()["project"] == ""
