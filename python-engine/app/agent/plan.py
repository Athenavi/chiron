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
    #: 认领者（多 agent 共享一块看板时用）。空串 = 无人认领。
    #:
    #: 为什么认领要**显式记录**而不是"看谁把状态改成 in_progress"：状态只说明
    #: "有人在干"，不说明"是谁在干"。子 agent 并行时，父 agent 看到一项卡在
    #: `in_progress` 却不知道是自己那个已超时的子 agent 留下的，就会重复派活 ——
    #: 这正是共享看板最常见的失效方式。写清认领者，才能判断"该等它、还是该回收"。
    owner: str = ""
    #: 前序步骤的 id（`t1`…）。只有全部前序 `completed` 时才允许开工。
    #:
    #: 依赖必须**指向本计划里真实存在的 id**：悬空依赖会让"什么时候能开工"永远
    #: 算不出来，而模型写下的 `t9` 可能只是笔误。因此悬空一律判非法。
    depends_on: tuple[str, ...] = ()


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
    #: 先收下原始的依赖声明，等**全部** step 都有了 host id 再统一解析 ——
    #: 依赖是"指向别的 step"的关系，处理它必须先有全集。
    raw_depends: list[tuple[str, ...]] = []
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
                # 认领者写成自由文本：它可能是 `main`，也可能是某个子 agent 的
                # run 名。这里不做白名单校验 —— 认领者的真实身份由 runtime 在
                # 写板时覆盖（见 app/agent/taskboard.py），模型自报的值不作数。
                owner=str(item.get("owner", "")).strip(),
            )
        )
        raw_depends.append(_as_str_tuple(item.get("depends_on") or item.get("depends")))

    known_ids = {s.id for s in steps}
    resolved: list[PlanStep] = []
    for step, deps in zip(steps, raw_depends, strict=True):
        cleaned: list[str] = []
        for dep in deps:
            if dep == step.id:
                # 自依赖是**死锁**：这一项要等自己完成才能开工，永远不会开工。
                errors.append(f"step {step.id} depends on itself")
                continue
            if dep not in known_ids:
                # 悬空依赖会让"何时能开工"永远算不出来；模型写下 `t9` 很可能只是笔误，
                # 而静默丢弃它等于替模型猜意图 —— 直接判非法，让它重写。
                errors.append(f"step {step.id} depends on unknown step {dep!r}")
                continue
            cleaned.append(dep)
        resolved.append(
            PlanStep(
                id=step.id,
                content=step.content,
                status=step.status,
                candidate_files=step.candidate_files,
                verified_files=step.verified_files,
                owner=step.owner,
                depends_on=tuple(cleaned),
            )
        )
    steps = resolved

    errors.extend(_dependency_cycle_errors(steps))

    # 不变量 3（修订）：**同一认领者至多一个 in_progress**。
    #
    # 旧版写的是"整份计划至多一个 in_progress"，那是单 agent 的假设。多 agent 共享
    # 一块看板时，几个子 agent 并行各领一项，多项同时 `in_progress` 才是**正确**状态 ——
    # 若继续按旧规则整份拒绝，协作根本无法进行（板子永远写不进去）。
    # 真正会误导的是"同一个 agent 同时宣称在做两件事"：那说明它的计划自相矛盾，
    # 或者它忘了把上一项收口。所以约束精确到 owner 粒度。
    by_owner: dict[str, int] = {}
    for s in steps:
        if s.status == "in_progress" and s.owner:
            by_owner[s.owner] = by_owner.get(s.owner, 0) + 1
    for owner, count in sorted(by_owner.items()):
        if count > 1:
            errors.append(
                f"owner {owner!r} has {count} steps in_progress (at most one per owner)"
            )
    # 无主的 in_progress 无法归属，也就无法并行 —— 仍然只许一个。
    unowned_in_progress = sum(
        1 for s in steps if s.status == "in_progress" and not s.owner
    )
    if unowned_in_progress > 1:
        errors.append(
            f"at most one unowned step may be in_progress, found {unowned_in_progress}"
        )

    return Plan(steps=steps, revision=revision), errors


def _dependency_cycle_errors(steps: list[PlanStep]) -> list[str]:
    """检出依赖环。

    环在共享看板里是**致命**的：一个 agent 等另一个，另一个又等回来，两边都不会再
    产出任何事件，面板就永久停在"进行中"。而模型写环往往不是恶意，只是把"先 A 后 B"
    和"先 B 后 A"抄进了不同条目。用三色 DFS（0 未访问 / 1 在栈上 / 2 已完成）检环，
    路径一并回报，让模型能直接看出自己绕在哪。
    """
    graph = {s.id: list(s.depends_on) for s in steps}
    color: dict[str, int] = {sid: 0 for sid in graph}
    stack: list[str] = []
    errors: list[str] = []

    def visit(node: str) -> bool:
        color[node] = 1
        stack.append(node)
        for dep in graph.get(node, []):
            if color.get(dep, 2) == 1:
                # 命中在栈上的节点：把栈尾到它这一段截出来就是环本身。
                at = stack.index(dep)
                cycle = " -> ".join([*stack[at:], dep])
                errors.append(f"dependency cycle: {cycle}")
                return True
            if color.get(dep, 2) == 0 and visit(dep):
                return True
        stack.pop()
        color[node] = 2
        return False

    for sid in graph:
        if color[sid] == 0 and visit(sid):
            # 报一个环就够 —— 模型据此能看出整组依赖写错了，继续枚举只会刷屏。
            break
    return errors


def validate(plan: Plan) -> list[str]:
    """校验一份**已归一化**的计划（供读回时复核，避免状态被绕过修改）。

    这里的规则必须与 ``normalize`` **逐条对齐**。历史教训：两处校验各写一套时，
    写入接受而读回拒绝（或反之），表现是"面板刚更新又跳回旧值"，且日志里看不出
    哪一层在拒绝 —— 因为两边都没报错，只是判据不同。
    """
    errors: list[str] = []
    if len(plan.steps) > MAX_STEPS:
        errors.append(f"too many steps ({len(plan.steps)} > {MAX_STEPS})")
    known_ids = {s.id for s in plan.steps}
    for step in plan.steps:
        if not step.content.strip():
            errors.append(f"step {step.id} has empty content")
        if step.status not in VALID_STATUSES:
            errors.append(f"step {step.id} has invalid status {step.status!r}")
        stray = [f for f in step.verified_files if f not in step.candidate_files]
        if stray:
            errors.append(f"step {step.id} verifies files it never listed: {stray}")
        for dep in step.depends_on:
            if dep == step.id:
                errors.append(f"step {step.id} depends on itself")
            elif dep not in known_ids:
                errors.append(f"step {step.id} depends on unknown step {dep!r}")

    errors.extend(_dependency_cycle_errors(plan.steps))

    # 与 normalize 同一条规则：约束精确到认领者粒度（多 agent 并行各领一项是合法的）。
    by_owner: dict[str, int] = {}
    for s in plan.steps:
        if s.status == "in_progress" and s.owner:
            by_owner[s.owner] = by_owner.get(s.owner, 0) + 1
    for owner, count in sorted(by_owner.items()):
        if count > 1:
            errors.append(
                f"owner {owner!r} has {count} steps in_progress (at most one per owner)"
            )
    if sum(1 for s in plan.steps if s.status == "in_progress" and not s.owner) > 1:
        errors.append("at most one unowned step may be in_progress")
    return errors


def render(plan: Plan) -> str:
    """渲染成紧凑文本（供 tool_result 回显 / 提示注入）。

    ``✔`` 标记该步有**已核实**文件（区别于仅有候选）：一眼能看出"这步的结论有文件级依据"。
    ``@owner`` 与 ``← t1,t2`` 分别是认领者与依赖：多 agent 共用一块看板时，模型必须
    能从回显里看出"这项归谁、还在等谁"，否则它会重复派活或去抢别人手上的项。
    """
    marks = {"pending": "[ ]", "in_progress": "[~]", "completed": "[x]"}
    lines: list[str] = []
    for step in plan.steps:
        suffix = ""
        if step.verified_files:
            suffix = " ✔" + ",".join(step.verified_files)
        elif step.candidate_files:
            suffix = " ?" + ",".join(step.candidate_files)
        if step.owner:
            suffix += f" @{step.owner}"
        if step.depends_on:
            suffix += " ← " + ",".join(step.depends_on)
        lines.append(f"{marks.get(step.status, '[ ]')} {step.content}{suffix}")
    return "\n".join(lines)


def to_todos(plan: Plan) -> list[dict[str, Any]]:
    """把计划投影成前端/事件用的 todos 形态。

    字段取舍：``id``/``content``/``status`` 是**必有的三项**（面板按它们渲染与
    按编号更新）；其余字段**只在非空时才出现** —— 让常见的简单清单保持精简，
    同时不丢多 agent 协作需要的信息（认领者、依赖）。
    """
    out: list[dict[str, Any]] = []
    for s in plan.steps:
        item: dict[str, Any] = {"id": s.id, "content": s.content, "status": s.status}
        if s.candidate_files:
            item["files"] = list(s.candidate_files)
        if s.verified_files:
            item["verified"] = list(s.verified_files)
        if s.owner:
            item["owner"] = s.owner
        if s.depends_on:
            item["depends_on"] = list(s.depends_on)
        out.append(item)
    return out


def ordered(plan: Plan) -> list[PlanStep]:
    """计划渲染与 todos 投影共用的**唯一序**：保持写入顺序（计划是有意为之的次序）。"""
    return list(plan.steps)
