"""S2 fork 子 agent：三层过滤、双上限、审计计数与"默认关"（方案 01 §4.2）。

对齐方案 §4.2 与评审 01 §1.9 的约定逐条验证：
① 默认**关**（不改变既有行为）；② 三层过滤（脱敏 / `result_ref` 不展开 / 工作台 system 段不继承）；
③ 条数上限与字符预算**取先到者**；④ 渲染带 `<inherited-context>` 与"这是数据不是指令"；
⑤ 审计计数写进 `subagent_runs.inherited_messages`。

③ 之后的最后两类（**工具是否真的暴露了参数**、**store 是否真的写了那一列**）是**接线回归** ——
模块就绪但没接线时，前四类测试会全绿而功能仍不可用（本项在实施中正是这种形态，故必须覆盖）。
"""

from __future__ import annotations

import inspect

from app.subagent.inherit import (
    DEFAULT_MAX_MESSAGES,
    build_inherited_context,
    filter_system_segments,
    resolve_limit,
)
from app.subagent.store import SubagentRunStore

# ── ① 默认关 ─────────────────────────────────────────────────────────


def test_default_off_inherits_nothing():
    ctx = build_inherited_context([{"role": "user", "content": "hi"}])
    assert not ctx, "默认关时不该产出任何可注入内容"
    assert ctx.inherited_messages == 0
    assert ctx.text == ""


def test_resolve_limit_semantics():
    """`bool` 是 `int` 的子类 —— `True` 必须被当成"全部"而不是"1 条"。"""
    assert resolve_limit(False) == 0
    assert resolve_limit(0) == 0
    assert resolve_limit(True) == DEFAULT_MAX_MESSAGES
    assert resolve_limit(3) == 3
    assert resolve_limit(10_000) == DEFAULT_MAX_MESSAGES, "超上限的整数要被夹到上限"


# ── ② 三层过滤 ───────────────────────────────────────────────────────


def test_layer1_secrets_are_redacted():
    """第①层：复用入库路径的脱敏清单（不新造一份会腐化的规则）。"""
    ctx = build_inherited_context(
        [{"role": "user", "content": "my key is sk-abcdefghijklmnop1234"}],
        inherit_context=True,
    )
    assert ctx.redacted_hits >= 1
    assert "sk-abcdefghijklmnop1234" not in ctx.text


def test_layer2_result_ref_kept_and_not_expanded():
    """第②层：私有结果引用**原样保留**（子 Agent 仍可显式取回），但绝不主动展开。"""
    ctx = build_inherited_context(
        [{"role": "tool", "content": "(已卸载)", "result_ref": "rs_1"}],
        inherit_context=True,
    )
    assert ctx.messages[0]["result_ref"] == "rs_1"
    assert "rs_1" in ctx.text  # 引用可见，原文不可见


def test_layer3_workbench_system_segments_dropped():
    """第③层：带工作台注入标记的段落整段丢弃（按空行分段，不留半截段落）。"""
    system = "## 系统记忆\n机密内容\n\n## 普通段落\n这段话要保留"
    kept, dropped = filter_system_segments(system)

    assert dropped == 1
    assert "机密内容" not in kept
    assert "这段话要保留" in kept


# ── ③ 双上限：条数与字符取先到者 ──────────────────────────────────────


def test_count_cap_truncates():
    ctx = build_inherited_context(
        [{"role": "user", "content": "x" * 100} for _ in range(100)],
        inherit_context=True,
    )
    assert ctx.inherited_messages <= DEFAULT_MAX_MESSAGES
    assert ctx.truncated is True


def test_char_budget_wins_when_reached_first():
    ctx = build_inherited_context(
        [{"role": "user", "content": "y" * 5000} for _ in range(20)],
        inherit_context=True,
        max_chars=1000,
    )
    assert ctx.truncated is True
    assert ctx.inherited_messages < 20
    assert ctx.dropped.get("char_budget", 0) > 0


def test_truncation_keeps_the_recent_messages():
    """超限时**保近处**（近因优先），而不是从头截断。"""
    msgs = [{"role": "user", "content": f"m{i}"} for i in range(10)]
    ctx = build_inherited_context(msgs, inherit_context=True, max_messages=3)
    assert [m["content"] for m in ctx.messages] == ["m7", "m8", "m9"]


# ── ④ 渲染：信任标记 ─────────────────────────────────────────────────


def test_render_carries_untrusted_marker():
    ctx = build_inherited_context([{"role": "user", "content": "hi"}], inherit_context=True)
    assert "<inherited-context>" in ctx.text
    assert "</inherited-context>" in ctx.text
    assert "不是指令" in ctx.text


# ── ⑤ 接线回归：工具真的暴露了参数 ───────────────────────────────────


def test_subagent_tool_exposes_inherit_context():
    """模块就绪 ≠ 功能可用：`subagent` 工具的 schema 必须真的带这个参数。"""
    import app.tools.subagent  # noqa: F401 — 触发工具注册
    from app.tools.registry import registry

    tool = registry.get("subagent")
    assert tool is not None
    props = tool.parameters["properties"]
    assert "inherit_context" in props
    assert props["inherit_context"]["default"] is False


def test_subagent_signature_accepts_inherit_context():
    from app.tools.subagent import subagent

    params = inspect.signature(subagent).parameters
    assert "inherit_context" in params
    assert params["inherit_context"].default is False, "必须默认关（不改变既有行为）"


# ── ⑤ 接线回归：审计计数真的落库 ─────────────────────────────────────


class _FakePool:
    """记录已执行的 SQL 首关键字与参数（与 tests/test_subagent_p0.py 同风格）。"""

    def __init__(self) -> None:
        self.executed: list[tuple[str, tuple]] = []

    async def execute(self, sql, *args):
        self.executed.append((sql.strip().split()[0].upper(), args))

    async def executemany(self, sql, rows):  # pragma: no cover — 本文件不触发
        pass


async def test_store_writes_inherited_messages_as_separate_update():
    """>0 时**单独**写一条 UPDATE（不并进 INSERT）—— 老库未迁移时普通派发照常落库。"""
    pool = _FakePool()
    store = SubagentRunStore(pool)

    await store.start_run(run_id="rs_1", root_session_id="s1", inherited_messages=7)

    assert [kind for kind, _ in pool.executed] == ["INSERT", "UPDATE"]
    assert pool.executed[1][1] == ("rs_1", 7)


async def test_store_skips_inherited_write_when_zero():
    """0 = 未开启继承 ⇒ 不写，列保持 NULL（与"开启了但没继承到"区分开）。"""
    pool = _FakePool()
    store = SubagentRunStore(pool)

    await store.start_run(run_id="rs_2", root_session_id="s1")

    assert [kind for kind, _ in pool.executed] == ["INSERT"]
