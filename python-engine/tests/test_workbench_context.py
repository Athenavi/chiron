"""工作台上下文多值读取与合并口径的测试。

覆盖 F3（多值上下文）里最容易出错、也最不该靠人工核对的两处纯逻辑：
``context_ids`` 的"多值优先 / 单值回退"，与 ``merge_by_quota`` 的轮询配额。

背景：前端 ``WorkbenchQuickStart`` 早就写了"（可多选）"且 ``Select`` 带
``mode="multiple"``，但后端从未读过多值 —— 勾 3 个知识库实际只有第 1 个生效。
这两处逻辑就是兑现那个承诺的落点。
"""

from __future__ import annotations

from app.agent.workbench_context import context_ids, merge_by_quota


class TestContextIds:
    def test_prefers_multi_value(self):
        assert context_ids({"kb_ids": ["a", "b"], "kb_id": "z"}, "kb") == ["a", "b"]

    def test_falls_back_to_single_value(self):
        # 向后兼容：老客户端与手写 URL（?kb=xxx）只发单值
        assert context_ids({"kb_id": "only"}, "kb") == ["only"]

    def test_multi_value_wins_without_merging_single(self):
        # 两者同时出现时以复数（更新的契约）为准，不合并 —— 否则顺序语义会变含糊
        assert context_ids({"kb_ids": ["a"], "kb_id": "z"}, "kb") == ["a"]

    def test_empty_context(self):
        assert context_ids({}, "kb") == []

    def test_dedupes_and_keeps_order(self):
        assert context_ids({"agent_ids": ["b", "a", "b"]}, "agent") == ["b", "a"]

    def test_trims_and_drops_blanks(self):
        assert context_ids({"skill_ids": ["  x  ", "", "   ", "y"]}, "skill") == ["x", "y"]

    def test_tolerates_non_string_entries(self):
        assert context_ids({"kb_ids": ["a", 42, None, {"x": 1}, "b"]}, "kb") == ["a", "b"]

    def test_accepts_scalar_string_multi_value(self):
        # 手写 URL 可能把多值序列化成单个字符串
        assert context_ids({"kb_ids": "solo"}, "kb") == ["solo"]

    def test_blank_single_value_is_dropped(self):
        assert context_ids({"kb_id": "   "}, "kb") == []

    def test_handles_non_dict_context(self):
        assert context_ids(None, "kb") == []
        assert context_ids("not-a-dict", "kb") == []
        assert context_ids([], "kb") == []

    def test_names_are_independent(self):
        ctx = {"kb_ids": ["k1"], "agent_ids": ["a1"], "skill_ids": ["w1"]}
        assert context_ids(ctx, "kb") == ["k1"]
        assert context_ids(ctx, "agent") == ["a1"]
        assert context_ids(ctx, "skill") == ["w1"]


class TestMergeByQuota:
    def test_round_robin_takes_one_from_each_source_first(self):
        assert merge_by_quota([["a1", "a2"], ["b1", "b2"]], 4) == ["a1", "b1", "a2", "b2"]

    def test_every_source_gets_a_slot_before_any_second_round(self):
        # 这正是选轮询而非全局按分数排序的理由：
        # 第三个库不该被前两个"高分"库挤掉（跨库分数本就不可比）
        assert merge_by_quota([["a1"], ["b1"], ["c1"]], 3) == ["a1", "b1", "c1"]

    def test_limit_truncates(self):
        assert merge_by_quota([["a1", "a2", "a3"]], 2) == ["a1", "a2"]

    def test_exhausted_source_is_skipped(self):
        assert merge_by_quota([["a1"], ["b1", "b2", "b3"]], 4) == ["a1", "b1", "b2", "b3"]

    def test_empty_inputs(self):
        assert merge_by_quota([], 5) == []
        assert merge_by_quota([[], []], 5) == []

    def test_non_positive_limit(self):
        assert merge_by_quota([["a1"]], 0) == []
        assert merge_by_quota([["a1"]], -1) == []

    def test_limit_larger_than_available(self):
        assert merge_by_quota([["a1"], ["b1"]], 100) == ["a1", "b1"]

    def test_preserves_within_source_order(self):
        # 每个库内部的原始排序必须保留（那是它自己的相关性排序）
        assert merge_by_quota([["a1", "a2", "a3"]], 3) == ["a1", "a2", "a3"]
