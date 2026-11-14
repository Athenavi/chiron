"""目标提醒（`app/agent/goal_context.py`）的行为约定。

这一层只做四件事：识别原始目标、确定性意图分类、渲染尾部提醒、以及**幂等**地
把提醒拼到消息尾部。它是"Agent 跑偏/提前收工"的第一道防线 —— 模型每轮都能在
消息尾部重新看到原始要求与当前拆解，不必指望它自己记住几十轮之前那句话。

不在这里测的：压缩交互（`test_context_overflow.py`）、计划看板持久化
（`test_todo_plan.py`）。本文件只锁本模块自己的契约。
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.agent import goal_context as gc


def _task(content: str = "") -> SimpleNamespace:
    return SimpleNamespace(content=content)


def _user(text: str) -> dict[str, object]:
    return {"role": "user", "content": text}


def _assistant(text: str) -> dict[str, object]:
    return {"role": "assistant", "content": text}


class TestIntentClassification:
    """意图分类必须是确定性的：同一句输入永远同一个标签。"""

    @pytest.mark.parametrize(
        ("text", "expected"),
        [
            ("这个接口报错 500，帮我修复", "fix"),
            ("实现一个导出 CSV 的功能", "build"),
            ("把这些重复代码重构一下", "refactor"),
            ("帮我审查一下这段代码有没有问题", "review"),
            ("为什么这里会慢？做个对比分析", "research"),
            ("部署到测试环境并发布", "ops"),
            ("把 README 文档写清楚", "docs"),
        ],
    )
    def test_keyword_rules_hit(self, text: str, expected: str) -> None:
        key, hits = gc.classify(text)
        assert key == expected
        assert hits, "命中标签时必须带回线索，供提醒里展示"

    def test_unknown_text_yields_no_intent(self) -> None:
        """一个都没命中时返回空标签 —— 调用方据此**不写**意图行。

        硬安一个"未归类"上去只是噪声，还会让模型误以为分类有意义。
        """
        key, hits = gc.classify("嗯")
        assert key == ""
        assert hits == ()

    def test_empty_text_is_safe(self) -> None:
        assert gc.classify("") == ("", ())
        assert gc.classify("   ") == ("", ())

    def test_classification_is_deterministic(self) -> None:
        text = "修复这个 bug，顺便重构相关代码"
        assert gc.classify(text) == gc.classify(text)

    def test_label_of_unknown_key(self) -> None:
        assert gc.label_of("nope") == gc._UNKNOWN_INTENT
        assert gc.label_of("fix") == "修复缺陷"


class TestExtractOriginalGoal:
    """原始目标 = 历史里第一条**真正的** user 消息。"""

    def test_takes_first_user_message(self) -> None:
        messages = [
            _assistant("先声明一句"),
            _user("把登录流程改成 OAuth"),
            _user("另外顺便看看性能"),
        ]
        assert gc.extract_original_goal(messages, _task()) == "把登录流程改成 OAuth"

    def test_falls_back_to_task_content(self) -> None:
        """新会话的正常形态：历史里还没有 user 消息。"""
        assert gc.extract_original_goal([], _task("新需求")) == "新需求"

    def test_skips_previous_reminder(self) -> None:
        """不跳过上一轮的提醒，提醒会被当成"原始目标"自我复读。"""
        messages = [
            _user(f"{gc.REMINDER_MARK}\n原始目标在这里\n</goal-reminder>"),
            _user("真正的第一句"),
        ]
        assert gc.extract_original_goal(messages, _task()) == "真正的第一句"

    def test_handles_parts_content(self) -> None:
        messages = [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": "带附件的请求"},
                    {"type": "image_url", "image_url": {"url": "data:..."}},
                ],
            }
        ]
        assert gc.extract_original_goal(messages, _task()) == "带附件的请求"

    def test_empty_when_nothing_available(self) -> None:
        assert gc.extract_original_goal([], _task("")) == ""


class TestClip:
    """超长目标保头保尾：约束在头，验收条件常在尾。"""

    def test_short_text_untouched(self) -> None:
        assert gc.clip("短目标") == "短目标"

    def test_long_text_keeps_head_and_tail(self) -> None:
        text = "头" * 1500 + "中" * 3000 + "尾" * 1500
        out = gc.clip(text, limit=2000)
        assert len(out) <= 2000 + 16
        assert out.startswith("头")
        assert out.endswith("尾")
        assert "……" in out


class TestRender:
    """提醒必须同时交代：原始目标、意图、当前进度、以及没拆解时的动作要求。"""

    def _ctx(self, text: str = "实现导出功能") -> gc.GoalContext:
        return gc.build([_user(text)], _task())

    def test_has_no_content_returns_empty(self) -> None:
        """取不到目标就不注入 —— 与引入本模块前逐字相同，零行为变化。"""
        assert gc.build([], _task("")).render() == ""

    def test_wrapped_in_marker(self) -> None:
        block = self._ctx().render()
        assert block.startswith(gc.REMINDER_MARK)
        assert block.rstrip().endswith("</goal-reminder>")
        assert "实现导出功能" in block

    def test_intent_line_present(self) -> None:
        block = self._ctx("实现导出功能").render()
        assert "识别到的意图" in block
        assert "实现功能" in block

    def test_without_plan_asks_for_decomposition(self) -> None:
        """没有清单时必须**要求先拆解**，这是"不会规划"的直接对策。"""
        block = self._ctx().render()
        assert "先" in block and "todo_write" in block
        assert "拆成" in block

    def test_with_plan_embeds_it(self) -> None:
        block = self._ctx().render("<current-plan>进度：1/3</current-plan>")
        assert "<current-plan>" in block
        assert "当前拆解与进度" in block
        assert "先用 todo_write" not in block

    def test_always_carries_delegation_guidance(self) -> None:
        """委派规则必须每轮都在 —— 无条件鼓励委派会显著抬高成本。"""
        for plan in ("", "<current-plan>x</current-plan>"):
            block = self._ctx().render(plan)
            assert "subagent" in block
            assert "不要委派" in block


class TestWithReminderIdempotent:
    """`messages` 跨轮复用，提醒必须**幂等**拼接，否则会越堆越多。"""

    def test_appends_once(self) -> None:
        messages = [_user("目标")]
        once = gc.with_reminder(messages, "<goal-reminder>x</goal-reminder>")
        assert len(once) == 2
        assert once[-1]["role"] == "user"

    def test_repeated_calls_do_not_accumulate(self) -> None:
        messages = [_user("目标")]
        block = "<goal-reminder>x</goal-reminder>"
        for _ in range(5):
            messages = gc.with_reminder(messages, block)
        assert len(messages) == 2

    def test_empty_block_strips_stale_reminder(self) -> None:
        """取不到目标时应**摘掉**旧提醒，而不是留着上一轮的。"""
        messages = gc.with_reminder([_user("目标")], "<goal-reminder>old</goal-reminder>")
        cleaned = gc.with_reminder(messages, "")
        assert cleaned == [_user("目标")]

    def test_does_not_mutate_input(self) -> None:
        messages = [_user("目标")]
        gc.with_reminder(messages, "<goal-reminder>x</goal-reminder>")
        assert len(messages) == 1, "输入列表不该被就地修改"

    def test_user_truth_survives(self) -> None:
        """提醒是 role=user，摘除时不能误伤用户真的说过的话。"""
        messages = [_user("我看不到，请解释"), _user("第二句")]
        assert gc.strip_reminder(messages) == messages
