"""memory_slots：对话里按分类收窄长期记忆的注入范围。

语义约定：**未指定 = 全量注入**（不改变既有行为），指定分类才收窄；
``all`` 与未指定等价，服务端因此只有「全量」与「按分类」两态。
"""


from app.agent.workbench_context import selected_memory_slots
from app.memory.service import MemoryService
from tests.fakes import InMemoryProfileStore


class TestSelectedMemorySlots:
    def test_empty_context_means_no_filter(self):
        assert selected_memory_slots(None) == []
        assert selected_memory_slots({}) == []

    def test_slots_passed_through(self):
        assert selected_memory_slots({"memory_slots": ["preference", "fact"]}) == [
            "preference",
            "fact",
        ]

    def test_all_collapses_to_empty(self):
        """all 与「未指定」在服务端是同一状态，不该让下游多判断一个值。"""
        assert selected_memory_slots({"memory_slots": ["all"]}) == []
        assert selected_memory_slots({"memory_slots": ["all", "fact"]}) == []

    def test_single_string_form(self):
        assert selected_memory_slots({"memory_slots": "preference"}) == ["preference"]


class TestRecallSlots:
    async def test_default_injects_all(self):
        svc = MemoryService(store=InMemoryProfileStore())
        await svc.upsert("t1", "u1", "preference", "editor", "VSCode")
        await svc.upsert("t1", "u1", "fact", "team", "5 人")
        block = (await svc.recall("t1", "u1")).profile_block
        assert "editor" in block
        assert "team" in block

    async def test_slots_narrow_injection(self):
        svc = MemoryService(store=InMemoryProfileStore())
        await svc.upsert("t1", "u1", "preference", "editor", "VSCode")
        await svc.upsert("t1", "u1", "fact", "team", "5 人")
        block = (await svc.recall("t1", "u1", slots=["preference"])).profile_block
        assert "editor" in block
        assert "team" not in block

    async def test_unknown_slot_yields_no_content(self):
        svc = MemoryService(store=InMemoryProfileStore())
        await svc.upsert("t1", "u1", "fact", "team", "5 人")
        result = await svc.recall("t1", "u1", slots=["identity"])
        assert result.has_content is False
