"""C3 第二段：项目级记忆的**端到端隔离**（service → 工具 → HTTP）。

验收口径（方案 02 §4 · C3）：**同一用户的两个项目互不串记忆**。

隔离有四条独立通道，任何一条漏掉，都会让"隔离"看起来生效而实际串味：

1. **写入**：同名 key 在两个项目里是两条独立记忆（去重键含 project）；
2. **读取**：list / search / recall 只返回本项目条目；
3. **删除**：按 id / 按 key / 清空都限定在项目内；
4. **整理**：近重复检测与超限淘汰只在项目内进行（否则 A 的条目会被当成 B 的重复项删掉）。

另有一条**向后兼容**口径：不传 project = 未分组（空串），既有调用点行为不变。
"""

from __future__ import annotations

from typing import Any

import httpx
import pytest
from httpx import ASGITransport

from app.main import create_app
from app.memory.service import MemoryService
from app.tools import context as tool_context
from tests.fakes import InMemoryProfileStore


def _svc() -> MemoryService:
    return MemoryService(store=InMemoryProfileStore())


def _values(data: dict[str, Any]) -> list[Any]:
    """条目值（`list_entries` 用 `entries`、`search` 用 `results`）。"""
    items = data.get("entries") or data.get("results") or []
    return sorted(str(e.get("value")) for e in items)


# ── 写入 / 读取 ─────────────────────────────────────────────────────────


async def test_same_key_in_two_projects_are_independent():
    svc = _svc()

    await svc.upsert("t", "u", slot="fact", key="lang", value="中文", project="proj-a")
    await svc.upsert("t", "u", slot="fact", key="lang", value="English", project="proj-b")

    assert _values(await svc.list_entries("t", "u", project="proj-a")) == ["中文"]
    assert _values(await svc.list_entries("t", "u", project="proj-b")) == ["English"]


async def test_ungrouped_is_its_own_project():
    """不传 project = 未分组，与任何具名项目都互不可见（不是"能看见全部"）。"""
    svc = _svc()

    await svc.upsert("t", "u", slot="fact", key="k", value="未分组")
    await svc.upsert("t", "u", slot="fact", key="k", value="A", project="proj-a")

    assert _values(await svc.list_entries("t", "u")) == ["未分组"]
    assert _values(await svc.list_entries("t", "u", project="proj-a")) == ["A"]


async def test_search_does_not_leak_across_projects():
    svc = _svc()

    await svc.upsert("t", "u", slot="fact", key="lang", value="中文", project="proj-a")
    await svc.upsert("t", "u", slot="fact", key="lang", value="English", project="proj-b")

    hit_a = await svc.search("t", "u", "lang", project="proj-a")

    assert _values(hit_a) == ["中文"]


async def test_recall_only_includes_current_project():
    svc = _svc()

    await svc.upsert("t", "u", slot="preference", key="lang", value="中文", project="proj-a")
    await svc.upsert("t", "u", slot="preference", key="lang", value="English", project="proj-b")

    recalled_a = await svc.recall("t", "u", "lang", project="proj-a")
    recalled_b = await svc.recall("t", "u", "lang", project="proj-b")

    assert "中文" in recalled_a.profile_block
    assert "English" not in recalled_a.profile_block
    assert "English" in recalled_b.profile_block


# ── 冲突：跨项目同名**不是**冲突 ────────────────────────────────────────


async def test_cross_project_same_key_is_not_a_conflict():
    """两个项目的同名 key 是两条独立记忆 —— 不该触发"要覆盖你确认过的值"的登记。"""
    svc = _svc()

    await svc.upsert(
        "t", "u", slot="preference", key="theme", value="dark", project="proj-a"
    )
    result = await svc.upsert(
        "t", "u", slot="preference", key="theme", value="light", project="proj-b"
    )

    assert "conflict" not in result


async def test_same_project_same_key_still_conflicts():
    """反向保护：**同项目内**的覆盖仍然要登记冲突（别把隔离做成"啥都不冲突"）。"""
    svc = _svc()

    await svc.upsert("t", "u", slot="preference", key="theme", value="dark", project="proj-a")
    result = await svc.upsert(
        "t", "u", slot="preference", key="theme", value="light", source="derived",
        project="proj-a",
    )

    assert result.get("conflict"), "同项目内的覆盖仍须登记待裁决"


# ── 删除 ────────────────────────────────────────────────────────────────


async def test_forget_by_key_is_scoped():
    svc = _svc()
    await svc.upsert("t", "u", slot="fact", key="k", value="A", project="proj-a")
    await svc.upsert("t", "u", slot="fact", key="k", value="B", project="proj-b")

    assert await svc.forget_by_key("t", "u", "k", project="proj-a") == 1
    assert _values(await svc.list_entries("t", "u", project="proj-b")) == ["B"]
    assert await svc.forget_by_key("t", "u", "k", project="proj-b") == 1


async def test_clear_all_is_scoped():
    svc = _svc()
    await svc.upsert("t", "u", slot="fact", key="k", value="A", project="proj-a")
    await svc.upsert("t", "u", slot="fact", key="k", value="B", project="proj-b")

    assert await svc.clear_all("t", "u", project="proj-a") == 1
    assert _values(await svc.list_entries("t", "u", project="proj-b")) == ["B"]


async def test_delete_entry_is_scoped_by_id():
    """拿 A 项目的 id、却声明 B 项目 —— 不该删得动（按 id 也要过 project 这道关）。"""
    svc = _svc()
    await svc.upsert("t", "u", slot="fact", key="k", value="A", project="proj-a")
    entry = (await svc.list_entries("t", "u", project="proj-a"))["entries"][0]

    assert await svc.delete_entry("t", "u", entry["id"], project="proj-b") is False
    assert await svc.delete_entry("t", "u", entry["id"], project="proj-a") is True


# ── 整理 ────────────────────────────────────────────────────────────────


async def test_organize_is_scoped_to_one_project():
    svc = _svc()
    await svc.upsert("t", "u", slot="fact", key="k", value="A", project="proj-a")
    await svc.upsert("t", "u", slot="fact", key="k", value="B", project="proj-b")

    await svc.organize_now("t", "u", project="proj-a")

    assert _values(await svc.list_entries("t", "u", project="proj-b")) == ["B"], (
        "整理另一个项目不该动到这里"
    )


def test_organize_single_flight_is_per_project():
    """单飞行按项目记账：A 在整理时，B 不该被判定成"已在运行"而永远排不上队。"""
    svc = _svc()
    svc._organize_state[("t", "u", "proj-a")] = {"running": True}

    assert svc.organize_status("t", "u", "proj-a")["running"] is True
    assert svc.organize_status("t", "u", "proj-b")["running"] is False


# ── 工具层：project 来自 tool context ───────────────────────────────────


async def test_remember_uses_context_project(monkeypatch):
    from app.tools import memory as memory_tools

    svc = _svc()
    monkeypatch.setattr(memory_tools, "_get_memory_service", lambda: svc)

    tool_context.set_tool_context(user_id="u", tenant_id="t", project="proj-a")
    out = await memory_tools.remember("lang", "中文")
    assert out.get("success") is True

    tool_context.set_tool_context(user_id="u", tenant_id="t", project="proj-b")
    assert (await svc.list_entries("t", "u", project="proj-b"))["total"] == 0
    assert (await svc.list_entries("t", "u", project="proj-a"))["total"] == 1


async def test_forget_uses_context_project(monkeypatch):
    from app.tools import memory as memory_tools

    svc = _svc()
    monkeypatch.setattr(memory_tools, "_get_memory_service", lambda: svc)

    tool_context.set_tool_context(user_id="u", tenant_id="t", project="proj-a")
    await memory_tools.remember("lang", "中文")

    # 换到另一个项目再遗忘 —— 不该动到 proj-a 的那条
    tool_context.set_tool_context(user_id="u", tenant_id="t", project="proj-b")
    out = await memory_tools.forget("lang")

    assert out.get("deleted") == 0
    assert (await svc.list_entries("t", "u", project="proj-a"))["total"] == 1


# ── HTTP 层：project 从 query 读 ────────────────────────────────────────


def _patch_api_service(monkeypatch: pytest.MonkeyPatch, svc: MemoryService) -> None:
    monkeypatch.setattr("app.api.memory.get_service", lambda: svc)


async def test_http_profile_is_project_scoped(monkeypatch):
    svc = _svc()
    _patch_api_service(monkeypatch, svc)
    params = {"user_id": "u", "tenant_id": "t"}

    async with httpx.AsyncClient(
        transport=ASGITransport(app=create_app()), base_url="http://t"
    ) as client:
        resp = await client.post(
            "/v1/memory/profile",
            json={"slot": "fact", "key": "k", "value": "A"},
            params={**params, "project": "proj-a"},
        )
        assert resp.status_code == 200

        in_a = await client.get("/v1/memory/profile", params={**params, "project": "proj-a"})
        in_b = await client.get("/v1/memory/profile", params={**params, "project": "proj-b"})

    assert in_a.json()["total"] == 1
    assert in_b.json()["total"] == 0, "HTTP 层绝不能把 A 项目的记忆返回给 B 项目"


async def test_http_clear_only_clears_that_project(monkeypatch):
    svc = _svc()
    _patch_api_service(monkeypatch, svc)
    params = {"user_id": "u", "tenant_id": "t"}
    await svc.upsert("t", "u", slot="fact", key="k", value="A", project="proj-a")
    await svc.upsert("t", "u", slot="fact", key="k", value="B", project="proj-b")

    async with httpx.AsyncClient(
        transport=ASGITransport(app=create_app()), base_url="http://t"
    ) as client:
        resp = await client.post(
            "/v1/memory/profile/clear",
            json={"confirm": True},
            params={**params, "project": "proj-a"},
        )

    assert resp.status_code == 200 and resp.json()["deleted"] == 1
    assert (await svc.list_entries("t", "u", project="proj-b"))["total"] == 1
