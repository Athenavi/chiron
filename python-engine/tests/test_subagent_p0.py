"""P0 回归测试：子 Agent 的脱敏、Profile 解析、落库缓冲与工具收窄。

覆盖设计文档 docs/subagent-design.md 的 P0 验收点：
* 敏感信息入库前被脱敏（D6 决策）；
* Profile 解析支持 llm_config.subagent 子对象与顶层写法，脏数据回落默认值；
* L0 step 批量写 + 脱敏计数 + 缺表降级（不阻断子 Agent）；
* 工具收窄：只读剥离写工具、白名单生效、深度耗尽时剥离委派类工具。
"""
from __future__ import annotations

import pytest

from app.agent.profile import (DEFAULT_MAX_DEPTH, ProfileSpec, parse_profile_row)
from app.agent.subagent_runner import (SubAgentRunner, _step_kind, _wrap_result)
from app.subagent.redact import redact_payload, redact_text
from app.subagent.store import SubagentRunStore

# ── 脱敏 ──


def test_redact_hits_known_secret_shapes():
    text = (
        "openai sk-abcdefghijklmnop1234 and aws AKIAIOSFODNN7EXAMPLE "
        "and password: hunter2xyz and gh token ghp_" + "a" * 30
    )
    out, hits = redact_text(text)
    assert hits >= 3
    assert "sk-abcdefghijklmnop1234" not in out
    assert "AKIAIOSFODNN7EXAMPLE" not in out
    assert "hunter2xyz" not in out
    assert "[REDACTED:openai_key]" in out


def test_redact_keeps_ordinary_text_and_bare_prefix():
    text = "说明：sk- 是 OpenAI 的前缀约定，正文里没有真实密钥"
    out, hits = redact_text(text)
    assert hits == 0 and out == text


def test_redact_handles_none_and_non_string():
    assert redact_text(None) == ("", 0)
    assert redact_text("") == ("", 0)


def test_redact_payload_recurses_structures():
    payload = {"task": "读 .env 里的 sk-abcdefghijklmnop1234", "files": ["AKIAIOSFODNN7EXAMPLE"]}
    out, hits = redact_payload(payload)
    assert hits == 2
    assert "sk-" not in out["task"] and "AKIA" not in out["files"][0]


# ── Profile 解析 ──


def _row(**overrides):
    row = {
        "id": "a1",
        "name": "reviewer",
        "description": "代码评审",
        "system_prompt": "你是评审员",
        "llm_config": {},
        "max_turns": 8,
        "timeout_seconds": 90,
        "skills": ["s1"],
        "plugins": [],
        "workflows": [],
        "kb_id": "kb1",
    }
    row.update(overrides)
    return row


def test_parse_profile_reads_subagent_object():
    spec = parse_profile_row(_row(llm_config={
        "model": "deepseek-pro",
        "effort": "high",
        "subagent": {
            "read_only": True,
            "allowed_tools": ["read_file", "grep"],
            "disallowed_tools": ["browser"],
            "max_depth": 2,
            "output_max_chars": 1500,
        },
    }))
    assert isinstance(spec, ProfileSpec)
    assert spec.read_only is True
    assert spec.allowed_tools == ("read_file", "grep")
    assert spec.disallowed_tools == ("browser",)
    assert spec.max_depth == 2
    assert spec.output_max_chars == 1500
    assert spec.llm_config() == {"model": "deepseek-pro", "effort": "high"}


def test_parse_profile_accepts_top_level_keys_and_defaults():
    spec = parse_profile_row(_row(llm_config={"read_only": "true", "max_depth": "0"}))
    assert spec.read_only is True
    # max_depth=0 表示不限深度（minimum=0 不被默认值覆盖）
    assert spec.max_depth == 0
    # 未提供时用默认值
    assert spec.output_max_chars > 0
    assert DEFAULT_MAX_DEPTH == 1


def test_parse_profile_survives_dirty_data():
    spec = parse_profile_row(_row(llm_config="not-json", max_turns="abc", skills="a,b"))
    assert spec.max_turns > 0          # 非数字回落默认
    assert spec.skills == ("a", "b")   # 逗号串容错
    assert spec.read_only is False


def test_profile_llm_config_omits_empty_values():
    assert ProfileSpec().llm_config() == {}


# ── 落库 ──


class _FakePool:
    def __init__(self):
        self.executed: list[tuple] = []
        self.batches: list[int] = []

    async def execute(self, sql, *args):
        self.executed.append((sql.strip().split()[0].upper(), args))

    async def executemany(self, sql, rows):
        self.batches.append(len(rows))


@pytest.mark.asyncio
async def test_store_batches_steps_and_reports_usage():
    pool = _FakePool()
    store = SubagentRunStore(pool)
    await store.start_run(run_id="rs_1", root_session_id="s1", task="t", profile_name="reviewer")
    for i in range(25):
        await store.add_step("rs_1", kind="message", content=f"step {i}")
    assert store.step_count("rs_1") == 25
    assert pool.batches == [20]           # 达到阈值即 flush，剩余在内存
    assert store.pending_step_count("rs_1") == 5

    await store.finish_run("rs_1", status="completed", summary="结论", input_tokens=10, output_tokens=5, steps=25)
    assert pool.batches == [20, 5]        # finish 时兜底 flush
    first_sql = pool.executed[0][0]
    assert first_sql == "INSERT"          # start_run 写 subagent_runs


@pytest.mark.asyncio
async def test_store_redacts_task_and_counts_hits():
    pool = _FakePool()
    store = SubagentRunStore(pool)
    await store.start_run(run_id="rs_2", root_session_id="s1", task="读 sk-abcdefghijklmnop1234")
    task_arg = pool.executed[0][1][9]     # RUN_INSERT_SQL 的 task 参数
    assert "sk-abcdefghijklmnop1234" not in task_arg


@pytest.mark.asyncio
async def test_store_degrades_on_missing_table():
    class _BrokenPool:
        async def execute(self, sql, *args):
            raise RuntimeError('relation "subagent_runs" does not exist')

        async def executemany(self, sql, rows):
            raise RuntimeError('relation "subagent_run_steps" does not exist')

    store = SubagentRunStore(_BrokenPool())
    await store.start_run(run_id="rs_3", root_session_id="s1", task="t")   # 不抛
    await store.add_step("rs_3", kind="message", content="x")              # 不抛
    await store.finish_run("rs_3", status="completed", summary="s")        # 不抛
    assert store.available is False       # 一次失败后本进程不再重试


# ── 工具收窄与包装 ──


def _runner(**kwargs):
    return SubAgentRunner(gateway=object(), depth=kwargs.pop("depth", 0), **kwargs)


def test_resolve_tools_no_profile_defaults_to_read_only():
    """无 Profile 时**默认只读**（含前台派发）—— 写/执行必须显式 allow_write。

    为什么收紧：子 Agent 与父共享工作区，写/执行既可能互相踩，又会在 `tools_mode=auto`
    下每步都要用户确认（实测一次委派点了 9 次批准、每步都可能空等到 300s 超时）。
    """
    import app.tools.core  # noqa: F401
    import app.tools.edit_file  # noqa: F401
    import app.tools.subagent  # noqa: F401

    tools = _runner()._resolve_tools(None, "normal", 1, 3)
    assert tools is not None, "默认只读即意味着需要收窄工具集"
    names = {t["name"] for t in tools}
    assert "write_file" not in names and "shell_exec" not in names


def test_resolve_tools_allow_write_keeps_write_tools():
    """显式 allow_write=true 时才放开写/执行（否则委派无法完成"改代码"这类任务）。"""
    import app.tools.core  # noqa: F401

    tools = _runner(allow_write=True)._resolve_tools(None, "normal", 1, 3)
    if tools is not None:
        names = {t["name"] for t in tools}
        assert "write_file" in names


def test_resolve_tools_default_strips_delegation_tools():
    """默认策略（max_depth=1）：子 Agent 不能再委派 —— 无 Profile 时同样生效。

    这是相对旧版（可递归 3 层）的**有意行为变更**，见 docs/subagent-design.md §6；
    需要放开时在 Profile 的 llm_config.subagent.max_depth 显式调大。
    """
    import app.tools.core  # noqa: F401
    import app.tools.subagent  # noqa: F401

    tools = _runner()._resolve_tools(None, "normal", 1, DEFAULT_MAX_DEPTH)
    assert tools is not None
    assert "subagent" not in {t["name"] for t in tools}


def test_resolve_tools_read_only_strips_write_tools():
    import app.tools.core  # noqa: F401  确保核心工具已注册
    import app.tools.edit_file  # noqa: F401
    import app.tools.subagent  # noqa: F401

    spec = ProfileSpec(id="p1", name="reviewer", read_only=True)
    tools = _runner()._resolve_tools(spec, "normal", 1, 1)
    assert tools is not None
    names = {t["name"] for t in tools}
    assert "write_file" not in names and "edit_file" not in names
    assert "subagent" not in names        # 默认禁止再委派
    assert {"name", "description", "parameters"} <= set(tools[0].keys())


def test_resolve_tools_whitelist_wins():
    import app.tools.core  # noqa: F401

    spec = ProfileSpec(id="p1", name="reader", allowed_tools=("read_file", "grep_files"))
    tools = _runner()._resolve_tools(spec, "normal", 1, 1)
    names = {t["name"] for t in tools}
    assert names <= {"read_file", "grep_files"}


def test_resolve_tools_blacklist_and_delegate_control():
    import app.tools.core  # noqa: F401

    spec = ProfileSpec(id="p1", name="x", disallowed_tools=("web_fetch",))
    tools = _runner()._resolve_tools(spec, "normal", 1, 1)
    names = {t["name"] for t in tools}
    assert "web_fetch" not in names


def test_wrap_result_marks_untrusted_and_indents():
    wrapped = _wrap_result("rs_1", "reviewer", "completed", "结论：ok", False)
    lines = wrapped.splitlines()
    assert lines[0].startswith('<subagent-result run_id="rs_1"')
    assert "数据" in lines[1]             # 不可信标记
    assert lines[2].startswith("  ")      # 正文缩进
    assert lines[-1] == "</subagent-result>"


def test_step_kind_mapping_is_bounded():
    assert _step_kind("text") == "message"
    assert _step_kind("tool_call") == "tool_call"
    # approval 有**独立** kind（P3-后续）：历史回放据此还原成 subagent.approval，
    # 前端才能渲染可点击的审批卡片 —— 落成 notice 就只剩一行文字，看得到却批不了。
    assert _step_kind("approval") == "approval"
    # ask 同理（P4 后续）：回放要能还原成**可回答**的提问卡片（前端复用 AskCard）
    assert _step_kind("ask") == "ask"
    assert _step_kind("未知类型") == "notice"
