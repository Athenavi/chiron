"""S1：compiled 子 agent（target 协议 + 白名单注册表，方案 01 §4.1）。

对齐方案 §4.1 的约定：
① `target="profile:<id>"` 与 `profile=<id>` 走**同一条**子会话路径；
② 只有**内置**目标 —— 注册表（`BUILTIN_TARGET_PREFIXES` + 显式 `builtin` 闸门）不接受
   白名单外前缀，仓库里也不存在"用户上传可调用对象"的入口；
③ `skill:` / `workflow:` 复用既有执行入口（`skill_run` / `run_workflow`），不新造通道；
④ 非法 / 未知 target 给**明确错误**，不静默退回默认行为；
⑤ `target=` 是**同步**语义（需要后台请用 `profile=`）。
"""

from __future__ import annotations

import inspect
from typing import Any

from app.agent.subagent_runner import SubagentRunResult
from app.subagent.registry_targets import (
    BUILTIN_TARGET_PREFIXES,
    ProfileTarget,
    SkillTarget,
    TargetRegistry,
    WorkflowTarget,
    target_registry,
)
from app.subagent.target import SubagentContext, parse_target_spec
from app.tools.subagent import subagent

# ── 规格解析与注册表（白名单） ───────────────────────────────────────────


def test_parse_target_spec_shape():
    assert parse_target_spec("profile:abc") == ("profile", "abc")
    assert parse_target_spec("SKILL:foo") == ("skill", "foo")
    assert parse_target_spec("  workflow:w1  ") == ("workflow", "w1")
    # 无冒号 → 前缀为空（由注册表报"格式不对"，而不是"前缀不认识"）
    assert parse_target_spec("noseparator") == ("", "noseparator")
    assert parse_target_spec("") == ("", "")


def test_registry_rejects_unknown_prefix_and_non_builtin():
    """② 白名单：前缀不在册、或未声明为内置 → 一律拒绝。"""
    reg = TargetRegistry()
    assert reg.register("evil", ProfileTarget, builtin=True) is False
    assert reg.register("profile", ProfileTarget) is False
    assert reg.prefixes == ()


def test_builtin_registry_is_closed():
    """② 安全：内置单例只含三个前缀，且**没有**任何方式加进第四个。

    想加新前缀只能改 `BUILTIN_TARGET_PREFIXES` 常量（= 改代码、过 review），
    这就是"不提供用户注册可调用对象通道"的落点。
    """
    assert set(target_registry.prefixes) == {"profile", "skill", "workflow", "remote"}
    assert set(BUILTIN_TARGET_PREFIXES) == {"profile", "skill", "workflow", "remote"}
    assert target_registry.register("http_callback", SkillTarget, builtin=True) is False


def test_registry_resolve_messages_are_actionable():
    reg = TargetRegistry()
    reg.register("profile", ProfileTarget, builtin=True)

    for bad, needle in (("bogus:x", "unknown target prefix"), ("noseparator", "invalid target"), ("profile:", "missing its reference")):
        message = reg.resolve(bad)
        assert isinstance(message, str) and needle in message, (bad, message)

    resolved = reg.resolve("profile:reviewer")
    assert not isinstance(resolved, str)
    assert resolved.name == "profile:reviewer"


# ── 工具层：暴露、错误路径与同步约束 ────────────────────────────────────


def test_tool_exposes_target_as_keyword_only():
    import app.tools.subagent  # noqa: F401 — 触发工具注册
    from app.tools.registry import registry

    param = inspect.signature(subagent).parameters["target"]
    assert param.default == ""
    assert param.kind is inspect.Parameter.KEYWORD_ONLY, "新参数必须 keyword-only（不改已发布签名）"

    tool = registry.get("subagent")
    assert tool is not None
    assert "target" in tool.parameters["properties"]


def _ready_context(**overrides: Any) -> None:
    from app.tools.context import set_tool_context

    base: dict[str, Any] = {
        "session_id": "s1",
        "user_id": "u1",
        "tenant_id": "t1",
        "gateway": object(),
    }
    base.update(overrides)
    set_tool_context(**base)


async def test_unknown_target_is_an_error_not_a_silent_fallback():
    """④ 不静默退回默认子 Agent —— 那会让"写错的 target"看起来执行成功了。"""
    _ready_context()
    out = await subagent("task", run_in_background=False, target="nope:x")
    assert "error" in out
    assert "nope" in out["error"]


async def test_target_rejects_background_delegation():
    """⑤ target 是同步语义；后台请走 profile=。"""
    _ready_context()
    out = await subagent("task", run_in_background=True, target="profile:reviewer")
    assert "error" in out
    assert "synchronous" in out["error"]


# ── 三个内置目标 ────────────────────────────────────────────────────────


def _child_result(status: str = "completed", output: str = "ok") -> SubagentRunResult:
    return SubagentRunResult(run_id="rs_test", status=status, output=output)


async def test_profile_target_delegates_through_run_child():
    """① profile target 只是把 ref 转交给同一条子会话路径。"""
    calls: list[tuple[str, str]] = []

    async def run_child(task: str, *, profile_ref: str = "") -> SubagentRunResult:
        calls.append((task, profile_ref))
        return _child_result()

    result = await ProfileTarget("reviewer").run(
        "do it", SubagentContext(task="do it", run_child=run_child)
    )

    assert calls == [("do it", "reviewer")]
    assert result.status == "completed"


async def test_profile_target_without_runner_fails_loudly():
    result = await ProfileTarget("reviewer").run("do it", SubagentContext(task="do it"))
    assert result.status == "failed"
    assert "child runner" in result.error


async def test_skill_target_reuses_skill_run(monkeypatch):
    """③ 复用 `skill_run`；输出仍包 `<subagent-result>` 不可信标记。"""
    seen: dict[str, Any] = {}

    async def fake_skill_run(name: str, params: Any = "{}") -> dict[str, Any]:
        seen["name"] = name
        seen["params"] = params
        return {"output": "skill output", "skill": name, "exec_type": "prompt"}

    monkeypatch.setattr("app.tools.skill.skill_run", fake_skill_run)

    result = await SkillTarget("summarize").run("the task", SubagentContext(task="the task"))

    assert seen["name"] == "summarize"
    assert seen["params"]["input"] == "the task", "委派任务默认填入技能的 {input}"
    assert "<subagent-result" in result.output, "输出必须包不可信标记（带属性）"
    assert "skill output" in result.output
    assert result.status == "completed"


async def test_skill_target_propagates_failure_as_result(monkeypatch):
    """目标失败要以**结构化结果**回给父模型（而不是抛穿）。"""

    async def fake_skill_run(name: str, params: Any = "{}") -> dict[str, Any]:
        return {"error": "skill exploded"}

    monkeypatch.setattr("app.tools.skill.skill_run", fake_skill_run)

    result = await SkillTarget("x").run("t", SubagentContext(task="t"))
    assert result.status == "failed"
    assert "exploded" in result.error


class _FakePool:
    def __init__(self, row: Any) -> None:
        self._row = row
        self.queries: list[tuple[str, tuple]] = []

    async def fetchrow(self, sql: str, *args: Any) -> Any:
        self.queries.append((sql, args))
        return self._row


async def test_workflow_target_scopes_lookup_by_user(monkeypatch):
    """③+隔离：`workflow_graphs` 无 tenant_id 列，按全局唯一的 user_id 过滤。"""
    pool = _FakePool({"graph_json": {"nodes": [], "edges": []}})
    monkeypatch.setattr("app.db.get_pool", lambda: pool)

    async def fake_workflow_run(graph: Any, state: Any = None, name: str = "") -> dict[str, Any]:
        return {"workflow": "w", "instance_id": "i", "status": "completed", "output": {"node": "done"}}

    monkeypatch.setattr("app.workflow.tools.workflow_run", fake_workflow_run)

    result = await WorkflowTarget("wf_1").run("do", SubagentContext(task="do", user_id="u1"))

    sql, args = pool.queries[0]
    assert "user_id" in sql, "取图必须按 user_id 过滤（不能照抄其它调用点的宽松查询）"
    assert args == ("wf_1", "u1")
    assert result.status == "completed"
    assert "done" in result.output


async def test_workflow_target_reports_missing_or_bad_graph(monkeypatch):
    monkeypatch.setattr("app.db.get_pool", lambda: _FakePool(None))
    missing = await WorkflowTarget("wf_x").run("do", SubagentContext(task="do", user_id="u1"))
    assert missing.status == "failed" and "not found" in missing.error

    monkeypatch.setattr("app.db.get_pool", lambda: _FakePool({"graph_json": "not json"}))
    bad = await WorkflowTarget("wf_y").run("do", SubagentContext(task="do", user_id="u1"))
    assert bad.status == "failed" and "malformed" in bad.error
