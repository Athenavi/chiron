"""S1：内置可委派目标 + **白名单**注册表（方案 01 §4.1）。

三个内置目标：

| 目标 | 说明 |
|---|---|
| `ProfileTarget` | 现有 Profile 子会话 —— 走 Runner 的 `run_child`，**与 `profile=<ref>` 完全同一条路径**（行为不变） |
| `SkillTarget` | 把已安装技能当子 agent：复用 `skill_run` 的四类执行路径（prompt/python/shell/http），**不新造执行通道** |
| `WorkflowTarget` | 把已保存的工作流图当子 agent：复用 `run_workflow`（不新造调度） |

**白名单是代码层面的约束，不是约定**：`register()` 只接受 `BUILTIN_TARGET_PREFIXES` 里的
前缀、且要求显式 `builtin=True`（该参数只由**本模块**在导入时传入）。于是"注册任意可调用
对象"这条通道在本仓库里根本不存在 —— 这正是方案 §4.1 的安全要求。
"""

from __future__ import annotations

import json
import logging
from collections.abc import Callable
from typing import Any

from app.subagent.target import SubagentContext, SubagentTarget, parse_target_spec

logger = logging.getLogger(__name__)

#: 允许注册的目标前缀（白名单）。**新增前缀必须改这里** —— "只能内置"的落点。
BUILTIN_TARGET_PREFIXES: frozenset[str] = frozenset({"profile", "skill", "workflow", "remote"})

#: 目标工厂：`ref -> SubagentTarget`。
TargetFactory = Callable[[str], SubagentTarget]


def _result(
    *, status: str, output: str = "", error: str = "", profile: str = ""
) -> Any:
    """构造与子会话**同形**的结果（延迟 import：避免 `target → runner` 的 import 环）。

    `output` 复用 Runner 的 `_wrap_result`：技能/工作流的产物同样是**不可信数据**，
    必须带 `<subagent-result>` 标记与声明 —— "结果来自远端化/别处"这件事不因换了目标
    而改变。
    """
    from app.agent.subagent_runner import SubagentRunResult, _wrap_result, new_run_id

    run_id = new_run_id()
    wrapped = _wrap_result(run_id, profile, status, output, False) if output else ""
    return SubagentRunResult(
        run_id=run_id, status=status, output=wrapped, error=error, profile=profile
    )


def _failed(reason: str) -> Any:
    return _result(status="failed", error=reason)


class ProfileTarget:
    """把"某个 Profile 子会话"表达成一个 target（与 `profile=<ref>` 同路径）。"""

    prefix = "profile"

    def __init__(self, ref: str) -> None:
        self.ref = ref.strip()

    @property
    def name(self) -> str:
        return f"{self.prefix}:{self.ref}"

    @property
    def description(self) -> str:
        return "Delegate to a Profile-defined sub-agent (same path as profile=<ref>)"

    async def run(self, task: str, ctx: SubagentContext) -> Any:
        if ctx.run_child is None:
            return _failed("profile target requires a child runner")
        return await ctx.run_child(task, profile_ref=self.ref)


class SkillTarget:
    """把已安装技能当子 agent（复用 `skill_run`，含其沙箱 / SSRF / 体积治理）。"""

    prefix = "skill"

    def __init__(self, name: str) -> None:
        self.skill_name = name.strip()

    @property
    def name(self) -> str:
        return f"{self.prefix}:{self.skill_name}"

    @property
    def description(self) -> str:
        return "Run an installed skill as a sub-agent (prompt/python/shell/http)"

    async def run(self, task: str, ctx: SubagentContext) -> Any:
        from app.tools.skill import skill_run

        params = dict(ctx.args or {})
        # 技能模板里的 `{input}` 默认取**委派任务** —— 否则调用方必须自己重复一遍
        params.setdefault("input", task)
        try:
            result = await skill_run(self.skill_name, params)
        except Exception as e:  # noqa: BLE001 — 目标失败要以**结构化结果**回给父模型
            return _failed(f"skill {self.skill_name!r} raised: {e}")
        if not isinstance(result, dict):
            return _failed(f"skill {self.skill_name!r} returned an unexpected payload")
        if result.get("error"):
            return _failed(f"skill {self.skill_name!r}: {result['error']}")
        return _result(
            status="completed", output=str(result.get("output", "")), profile=self.name
        )


class WorkflowTarget:
    """把已保存的工作流图当子 agent 跑（复用 `run_workflow`）。

    **按 `user_id` 过滤取图**：`workflow_graphs` 表没有 `tenant_id` 列，而 `user_id` 是
    全局唯一标识 —— 所以"只取自己的图"既是隔离也是最小权限。现状别的调用点（工作流
    上下文注入）不过滤，那是既有面；这里**不复制**那个宽松做法。
    """

    prefix = "workflow"

    def __init__(self, workflow_id: str) -> None:
        self.workflow_id = workflow_id.strip()

    @property
    def name(self) -> str:
        return f"{self.prefix}:{self.workflow_id}"

    @property
    def description(self) -> str:
        return "Run a saved workflow graph as a sub-agent"

    async def run(self, task: str, ctx: SubagentContext) -> Any:
        from app.db import get_pool

        pool = get_pool()
        if pool is None:
            return _failed("workflow target requires a database connection")
        try:
            row = await pool.fetchrow(
                "SELECT graph_json FROM workflow_graphs WHERE id = $1 AND user_id = $2",
                self.workflow_id,
                ctx.user_id or None,
            )
        except Exception as e:  # noqa: BLE001
            return _failed(f"workflow lookup failed: {e}")
        if row is None:
            return _failed(f"workflow {self.workflow_id!r} not found for this user")
        graph = row["graph_json"]
        if isinstance(graph, str):
            try:
                graph = json.loads(graph)
            except json.JSONDecodeError:
                return _failed(f"workflow {self.workflow_id!r} has malformed graph_json")
        if not isinstance(graph, dict) or not graph:
            return _failed(f"workflow {self.workflow_id!r} has no usable graph_json")

        from app.workflow.tools import workflow_run

        state = dict(ctx.args or {})
        state.setdefault("task", task)
        try:
            result = await workflow_run(graph, state)
        except Exception as e:  # noqa: BLE001
            return _failed(f"workflow {self.workflow_id!r} raised: {e}")
        if not isinstance(result, dict):
            return _failed(f"workflow {self.workflow_id!r} returned an unexpected payload")
        if result.get("error"):
            return _failed(f"workflow {self.workflow_id!r}: {result['error']}")
        output = result.get("output")
        text = output if isinstance(output, str) else json.dumps(output, ensure_ascii=False, default=str)
        return _result(status="completed", output=text, profile=self.name)


class TargetRegistry:
    """目标注册表（**白名单**：只接受内置前缀与显式声明为内置的工厂）。"""

    def __init__(self) -> None:
        self._factories: dict[str, TargetFactory] = {}

    def register(
        self, prefix: str, factory: TargetFactory, *, builtin: bool = False
    ) -> bool:
        """登记 ``prefix`` 的工厂；拒绝非白名单前缀与未声明内置的调用。

        `builtin` 是**显式的意图闸门**（只有本模块导入时传 True），安全边界本身是
        "前缀白名单 + 本仓库没有用户可调的注册入口"这两条 —— 二者合起来即方案 §4.1 的
        "注册表白名单，只能注册仓库内置实现"。
        """
        key = (prefix or "").strip().lower()
        if key not in BUILTIN_TARGET_PREFIXES:
            logger.warning("target registry rejected unknown prefix %r", prefix)
            return False
        if not builtin:
            logger.warning("target registry rejected non-builtin registration %r", key)
            return False
        self._factories[key] = factory
        return True

    @property
    def prefixes(self) -> tuple[str, ...]:
        return tuple(sorted(self._factories))

    def allowed_specs(self) -> str:
        """错误提示里的可用形态清单（`profile:<id> | skill:<name> | workflow:<id>`）。"""
        return " | ".join(f"{p}:<ref>" for p in self.prefixes) or "(none)"

    def resolve(self, spec: str) -> SubagentTarget | str:
        """`"prefix:ref"` → 目标实例；返回 **str** 表示不可用（含原因）。"""
        prefix, ref = parse_target_spec(spec)
        if not prefix:
            return (
                f"invalid target {spec!r}: expected '<prefix>:<ref>' "
                f"(allowed: {self.allowed_specs()})"
            )
        factory = self._factories.get(prefix)
        if factory is None:
            return (
                f"unknown target prefix {prefix!r} (allowed: {self.allowed_specs()})"
            )
        if not ref:
            return f"target {spec!r} is missing its reference (expected '{prefix}:<ref>')"
        return factory(ref)


#: 进程内单例 —— 内置目标在**导入时**即完成登记（没有运行期注册入口）。
target_registry = TargetRegistry()
target_registry.register("profile", ProfileTarget, builtin=True)
target_registry.register("skill", SkillTarget, builtin=True)
target_registry.register("workflow", WorkflowTarget, builtin=True)

# S3：跨实例委派（`remote:<instance_id|auto>`）。**注册 ≠ 开放** —— 它自己还要查
# `remote_subagent_enabled`（默认关），所以这里注册只是为了"可被解析"。
from app.subagent.remote import RemoteSubagentTarget  # noqa: E402 — 置尾以避免 import 环

target_registry.register("remote", RemoteSubagentTarget, builtin=True)


# ── 给 Target 实现（含 S3 的 remote）复用的结果构造 ─────────────────────


def failed_result(reason: str) -> Any:
    """失败结果（公开别名：Target 实现不必去碰 `_failed`）。"""
    return _failed(reason)


def result_from_payload(payload: dict[str, Any]) -> Any:
    """把"工具载荷形态"的结果还原成 `SubagentRunResult`（S3：远端回流的适配层）。

    为什么需要它：远端引擎经内网 HTTP 返回的是 `to_tool_payload()` 的**载荷**（status /
    output / usage / result_ref…），而本侧 Target 协议要求 `SubagentRunResult`。这层适配让
    "远端结果"与"本地结果"对调用方完全一致（并保留 `result_ref`，便于沿
    `read_subagent_result` 追到远端那次运行）。
    """
    from app.agent.subagent_runner import SubagentRunResult, new_run_id

    raw_usage = payload.get("usage")
    usage: dict[str, Any] = raw_usage if isinstance(raw_usage, dict) else {}
    run_id = str(payload.get("result_ref") or payload.get("run_id") or "") or new_run_id()
    return SubagentRunResult(
        run_id=run_id,
        status=str(payload.get("status") or "completed"),
        output=str(payload.get("output") or ""),
        summary=str(payload.get("summary") or ""),
        input_tokens=int(usage.get("input_tokens") or 0),
        output_tokens=int(usage.get("output_tokens") or 0),
        steps=int(usage.get("steps") or 0),
        truncated=bool(payload.get("truncated")),
        error=str(payload.get("error") or ""),
        profile=str(payload.get("profile") or ""),
    )
