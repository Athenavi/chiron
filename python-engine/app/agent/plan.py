"""plan — 结构化任务计划**契约**（P3）。

对照实现：Reasonix `internal/runtime/plancontract`（"The plan is data, not prose"）。
Chiron 此前只有一个 `write_todos` 工具，计划是模型自己写的**自由 JSON**（`content`+`status`）——
host 不校验、不赋予 id、更不区分"我猜会改的文件"与"我真的读过/操作过的文件"。
于是同一份计划在不同轮次里可能互相矛盾，而"这个文件我已核实"永远只是模型的一句话。

本模块把计划变成**host 拥有的数据结构**，三条不变量：

1. **id / revision 由 host 赋予**（模型不能自选 id 来假装"同一条"）。
2. **候选文件与已验证文件分开字段**（``candidate_files`` vs ``verified_files``）——
   杜绝"把猜测当事实"：写进 ``verified_files`` 却不在 ``candidate_files`` 里，直接判非法。
3. **至多一个 step 处于 in_progress** —— 计划是"此刻在做什么"的单一真相。

本模块**纯逻辑**（无 IO、无并发），因此可单测；它不做计划评审、不下发指令 ——
那是 runtime 与 planner 的事。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

#: 步骤上限（与 app/tools/todo.py 的 MAX_TODOS 保持一致，防"计划把上下文撑爆"）。
MAX_STEPS = 50
#: 允许的状态（与 deepagents / todo.py 一致）。
VALID_STATUSES: frozenset[str] = frozenset({"pending", "in_progress", "completed"})


@dataclass(frozen=True)
class PlanStep:
    """计划的一步。"""

    id: str
    content: str
    status: str = "pending"
    #: 推断**会**触及的文件（尚未证实）。模型可以说"我打算改这些"。
    candidate_files: tuple[str, ...] = ()
    #: **实际读过 / 操作过**的文件。只有工具行为能证实，模型不能凭声明塞进来。
    verified_files: tuple[str, ...] = ()


@dataclass
class Plan:
    """一份计划（host 拥有）。"""

    steps: list[PlanStep] = field(default_factory=list)
    #: host 维护的修订号：每次写入 +1。模型不能设置它。
    revision: int = 0


def _as_str_tuple(value: Any) -> tuple[str, ...]:
    """把可选的文件列表归一化为去重、有序的字符串元组。"""
    if value is None:
        return ()
    if isinstance(value, str):
        value = [value]
    if not isinstance(value, (list, tuple)):
        return ()
    out: list[str] = []
    for item in value:
        text = str(item).strip()
        if text and text not in out:
            out.append(text)
    return tuple(out)


def normalize(raw: Any, *, revision: int = 0) -> tuple[Plan, list[str]]:
    """把模型给的原始 todos 归一化为 ``Plan``，并返回人类可读的校验错误列表。

    宽进严出：结构不对时**不抛异常**（模型可能写歪），而是收集错误、返回能解析的部分，
    由调用方决定是拒绝整份计划还是回灌错误。``errors`` 非空即表示这份计划**不可接受**。
    """
    errors: list[str] = []
    if raw is None:
        return Plan(steps=[], revision=revision), errors
    if not isinstance(raw, list):
        return Plan(steps=[], revision=revision), ["plan must be a list of steps"]
    if len(raw) > MAX_STEPS:
        errors.append(f"too many steps ({len(raw)} > {MAX_STEPS})")

    steps: list[PlanStep] = []
    for index, item in enumerate(raw):
        if not isinstance(item, dict):
            errors.append(f"step[{index}] must be an object")
            continue
        content = str(item.get("content", "")).strip()
        if not content:
            errors.append(f"step[{index}] is missing 'content'")
            continue
        status = str(item.get("status", "pending")).strip() or "pending"
        if status not in VALID_STATUSES:
            errors.append(
                f"step[{index}] has invalid status {status!r} (allowed: {sorted(VALID_STATUSES)})"
            )
            status = "pending"

        candidates = _as_str_tuple(item.get("files") or item.get("candidate_files"))
        verified = _as_str_tuple(item.get("verified") or item.get("verified_files"))
        # 不变量 2：不能"验证"一个从未列入候选的文件。
        stray = [f for f in verified if f not in candidates]
        if stray:
            errors.append(
                f"step[{index}] lists verified files not among its candidates: {stray}"
            )

        # host 赋 id（不变量 1）：模型给的不被采信为身份，仅作为可读后缀。
        # 用 `t` 前缀（`t1`/`t2`…）与既有 `write_todos` 的 id 形态保持一致，前端与测试
        # 都按它渲染，改前缀只会制造一次无谓的兼容性变更。
        steps.append(
            PlanStep(
                id=f"t{index + 1}",
                content=content,
                status=status,
                candidate_files=candidates,
                verified_files=verified,
            )
        )

    in_progress = [s for s in steps if s.status == "in_progress"]
    if len(in_progress) > 1:
        errors.append(
            f"at most one step may be in_progress, found {len(in_progress)}"
        )

    return Plan(steps=steps, revision=revision), errors


def validate(plan: Plan) -> list[str]:
    """校验一份**已归一化**的计划（供读回时复核，避免状态被绕过修改）。"""
    errors: list[str] = []
    if len(plan.steps) > MAX_STEPS:
        errors.append(f"too many steps ({len(plan.steps)} > {MAX_STEPS})")
    for step in plan.steps:
        if not step.content.strip():
            errors.append(f"step {step.id} has empty content")
        if step.status not in VALID_STATUSES:
            errors.append(f"step {step.id} has invalid status {step.status!r}")
        stray = [f for f in step.verified_files if f not in step.candidate_files]
        if stray:
            errors.append(f"step {step.id} verifies files it never listed: {stray}")
    if sum(1 for s in plan.steps if s.status == "in_progress") > 1:
        errors.append("at most one step may be in_progress")
    return errors


def render(plan: Plan) -> str:
    """渲染成紧凑文本（供 tool_result 回显 / 提示注入）。

    ``✔`` 标记该步有**已核实**文件（区别于仅有候选）：一眼能看出"这步的结论有文件级依据"。
    """
    marks = {"pending": "[ ]", "in_progress": "[~]", "completed": "[x]"}
    lines: list[str] = []
    for step in plan.steps:
        suffix = ""
        if step.verified_files:
            suffix = " ✔" + ",".join(step.verified_files)
        elif step.candidate_files:
            suffix = " ?" + ",".join(step.candidate_files)
        lines.append(f"{marks.get(step.status, '[ ]')} {step.content}{suffix}")
    return "\n".join(lines)


def to_todos(plan: Plan) -> list[dict[str, Any]]:
    """转成旧版 todos 形态（保持 ``write_todos`` 既有消费方不改口径）。"""
    return [
        {"id": s.id, "content": s.content, "status": s.status} for s in plan.steps
    ]


def ordered(plan: Plan) -> list[PlanStep]:
    """计划渲染与 todos 投影共用的**唯一序**：保持写入顺序（计划是有意为之的次序）。"""
    return list(plan.steps)
