"""S1：compiled 子 agent 的"目标协议"（进程内可委派目标，方案 01 §4.1）。

deepagents 用 ``CompiledSubAgent``（LangGraph runnable）表达"除了 Profile，还能把别的
可执行单元当子 agent 跑"。Chiron **不引 LangGraph**（决策 D1），改用 Python ``Protocol``
的等价物：一个瘦协议 + 一张**白名单注册表**。

**为什么必须白名单**：多租户引擎里"注册一个任意可调用对象"等价于"把内联代码执行引入
引擎进程"。所以注册表只接受**仓库内置**的实现（见 ``app/subagent/registry_targets.py``），
不提供"用户上传可调用对象"的通道 —— 租户要自定义逻辑有两条既有合法路径（MCP server
外部进程 / `plugin_runner` 子进程沙箱）。

**与 ``SubAgentRunner`` 的分工**：Runner 负责"怎么跑一个子 agent"（会话、预算、事件、
落库），Target 负责"**跑什么**"。因此上下文里带的是 Runner 提供的 ``run_child`` 回调 ——
Target 不自己造会话，也不直接碰 store。
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

if TYPE_CHECKING:  # 运行时不需要：避免 target 层反向依赖 runner（那会构成 import 环）
    from app.agent.subagent_runner import SubagentRunResult


@dataclass(frozen=True)
class SubagentContext:
    """一次委派的上下文（由 `app/tools/subagent.py` 装配，Target 只读）。"""

    task: str = ""
    tenant_id: str = ""
    user_id: str = ""
    session_id: str = ""
    depth: int = 0
    mode: str = "normal"
    #: "按标准子会话跑一轮"的回调（Runner 侧提供，返回 `SubagentRunResult`）。
    #: `ProfileTarget` 走它 ⇒ 与 `profile=` 参数**完全同一条路径**（行为不变）。
    run_child: Callable[..., Awaitable[Any]] | None = None
    #: 目标自己的参数（如 `target=skill:<name>` 的技能参数）。
    args: dict[str, Any] = field(default_factory=dict)


@runtime_checkable
class SubagentTarget(Protocol):
    """一个可被 `subagent(target=…)` 委派的目标。

    ``run`` 的返回值约定为 ``SubagentRunResult``（与既有子会话**同形**）—— 于是工具层、
    事件层与前端都不必区分"这是 Profile 子会话、技能还是工作流"。

    `name` / `description` 声明为 **property**（只读）：内置目标用 `@property` 实现，
    而 Protocol 里的可变属性声明**不**接受 property（mypy 会判不兼容）。
    """

    @property
    def name(self) -> str:
        """目标标识，形如 ``profile:reviewer`` / ``skill:foo`` / ``workflow:<id>``。"""
        ...

    @property
    def description(self) -> str: ...

    async def run(self, task: str, ctx: SubagentContext) -> SubagentRunResult: ...


def parse_target_spec(spec: str) -> tuple[str, str]:
    """把 ``"prefix:rest"`` 拆成 ``(prefix, rest)``；不含 ``:`` 时 prefix 为空串。

    只做**形状**校验（空值、缺前缀、缺主体），前缀是否受支持由注册表判定 ——
    两件事分开，错误信息才说得清"你写错了格式"还是"这个前缀不被允许"。
    """
    text = (spec or "").strip()
    if not text:
        return "", ""
    if ":" not in text:
        return "", text
    prefix, rest = text.split(":", 1)
    return prefix.strip().lower(), rest.strip()
