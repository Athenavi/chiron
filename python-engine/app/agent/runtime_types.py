"""Agent run 的数据类型与审批决策取值（从 `app/agent/runtime.py` 纯移动而来）。

为什么单独立一个模块：这些类型是 run 的**数据面**，被 runtime、测试与若干旁路模块引用；
它们与那个 3000+ 行的状态机放在同一文件里，任何读者都要先翻过半屏代码才能找到类型定义。

⚠️ 这里**只放类型与取值**：不含任何逻辑、不含 `AgentRuntime`。`runtime.py` 通过
`from app.agent.runtime_types import ...` 再导出，**既有 import 路径一律不变**。

再导出的写法说明：`runtime.py` 里那些逐条的 `X as X` 看着啰嗦，但它是两份约束的交集 ——
mypy 的 `no-implicit-reexport` 要求 `as` 别名才算"显式导出"；本仓库 ruff 的 isort 配置
（`combine-as-imports=false`）会把带别名的再导出拆成逐条。**不要合并成一行。**
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from typing import Any


@dataclass
class AgentTask:
    """Agent 任务定义"""

    id: str
    tenant_id: str
    user_id: str
    session_id: str
    content: str
    system_prompt: str = ""
    history: list[dict[str, Any]] = field(default_factory=list)
    tools: list[dict[str, Any]] = field(default_factory=list)
    llm_config: dict[str, Any] = field(default_factory=dict)
    max_turns: int = 5
    subagent_depth: int = 0  # S3: 委派深度（subagent 递归限制，MAX_DEPTH=3）
    #: 本次 run 的唯一凭据（由入口生成，与 Go 侧运行锁 / Redis 归属租约同源）。
    #: C1 批 3+ 的**接管 CAS** 靠它判定"要接管的那一行是不是我看到的那个 run"。
    run_token: str = ""
    #: 工作台上下文（前端 ChatView.buildContext 组装并经 Go 透传）：
    #: kb_id / agent / agent_id / skill_names[] / workflow_id。引擎侧按需消费。
    workbench_context: dict[str, Any] = field(default_factory=dict)
    #: C2：本轮召回的**记忆块**。它**不拼进 `system_prompt`**，而是作为一条独立的 system
    #: 消息插在稳定前缀之后 —— `system_prompt`（系统提示 + 技能目录）是逐字稳定的前缀，
    #: 各家 provider 的前缀缓存（OpenAI/DeepSeek 自动、Anthropic 显式）都靠它命中；
    #: 把每轮可能变化的记忆拼进去，会让**整段前缀**的缓存一起失效。
    memory_context: str = ""

    @classmethod
    def parse(cls, data: dict[str, Any]) -> AgentTask:
        """从字典解析任务"""
        return cls(
            id=data.get("task_id", ""),
            tenant_id=data.get("tenant_id", ""),
            user_id=data.get("user_id", ""),
            session_id=data.get("session_id", ""),
            content=data.get("content", ""),
            system_prompt=data.get("system_prompt", ""),
            history=data.get("history", []),
            tools=data.get("tools", []),
            llm_config=data.get("llm_config", {}),
            max_turns=data.get("max_turns", 10),
        )


@dataclass
class AgentEvent:
    """Agent 事件 - 支持链路追踪 (SaaS: 跨实例无状态扩展)"""

    type: str
    content: str = ""
    tool_call_id: str = ""
    tool_name: str = ""
    tool_arguments: str = ""
    #: ask_user 的建议答案（仅 type="ask" 事件使用）
    options: list[str] = field(default_factory=list)
    input_tokens: int = 0
    output_tokens: int = 0
    #: 命中提示词缓存的输入 token 数（会话统计里"缓存命中率"的分子）。
    cached_tokens: int = 0
    #: 本回合实际使用的模型名（供按轮记录模型；空 = 未取到）。
    model: str = ""
    error: str = ""
    timestamp: float = field(default_factory=time.time)
    # ── Trace ID (新增: 支持分布式链路追踪) ──
    trace_id: str = ""  # 单次用户请求的唯一标识
    span_name: str = ""  # 当前 span 名称 (llm_call / tool_execution / workflow_node)
    duration_ms: int = 0  # span 耗时 (毫秒)


@dataclass
class ApprovalTicket:
    """审批票据 —— **批准的必须是"这一次操作"**，而不是"这个 id"。

    ``tool_call_id`` 由模型生成（常见 ``call_1`` / ``call_2`` 这类可预测 id），跨轮次可能重复。
    只按 id 记决策就会出现这样的场景：第 1 轮请求批准 ``call_1``（用户未响应，决策键在 TTL 内
    残留），第 2 轮模型又发来 ``call_1``（这次是危险调用）—— 残留决策被它吃掉并执行。

    票据在**发起审批时**写入 Redis（``approval_req:{tool_call_id}``），在**执行前**读回复核
    （见 ``_second_check_approval``）：turn_id 或参数哈希对不上就拒绝（fail-closed）。
    """

    tool_call_id: str
    tool_name: str
    args_hash: str
    turn_id: str

    def to_json(self) -> str:
        return json.dumps(
            {
                "tool_call_id": self.tool_call_id,
                "tool_name": self.tool_name,
                "args_hash": self.args_hash,
                "turn_id": self.turn_id,
            },
            ensure_ascii=False,
            sort_keys=True,
        )

    @classmethod
    def from_json(cls, raw: str) -> ApprovalTicket | None:
        try:
            data = json.loads(raw or "{}")
        except Exception:  # noqa: BLE001 - 票据损坏按"无票据"处理（fail-closed）
            return None
        if not isinstance(data, dict) or not data.get("tool_call_id"):
            return None
        return cls(
            tool_call_id=str(data.get("tool_call_id", "")),
            tool_name=str(data.get("tool_name", "")),
            args_hash=str(data.get("args_hash", "")),
            turn_id=str(data.get("turn_id", "")),
        )


# ── 审批决策（C5：审批三态 approve / reject / **edit**）────────────────────

#: 决策取值。`edit` = "编辑后批准"（对位 deepagents `interrupt_on` 的 approve/edit/reject）。
DECISION_APPROVE = "approve"
DECISION_REJECT = "reject"
DECISION_EDIT = "edit"
VALID_DECISIONS: frozenset[str] = frozenset({DECISION_APPROVE, DECISION_REJECT, DECISION_EDIT})


@dataclass
class ApprovalDecision:
    """一次审批决策（跨副本经 Redis 决策键传递，见 `_write_approval_decision`）。

    `edit` 时 `arguments` 是**编辑后的完整参数对象**（整份替换，不是补丁）—— 用户改的是
    "将要执行的那一次调用"，因此二次校验与执行都以它为准（"批准的就是执行的"）。
    """

    decision: str
    arguments: dict[str, Any] | None = None
    #: 用户备注 / 拒绝理由（随决策一起跨副本传递，供审计留痕）
    reason: str = ""

    @property
    def approved(self) -> bool:
        """`edit` 也是批准（只是参数变了）。"""
        return self.decision in (DECISION_APPROVE, DECISION_EDIT)

    def to_json(self) -> str:
        payload: dict[str, Any] = {"decision": self.decision}
        if self.arguments is not None:
            payload["arguments"] = self.arguments
        if self.reason:
            payload["reason"] = self.reason
        return json.dumps(payload, ensure_ascii=False)

    @classmethod
    def from_raw(cls, raw: Any) -> ApprovalDecision | None:
        """解析决策载荷；**兼容旧格式**。

        已发布的前端只发 `approved: bool`，跨副本通道上写的是 `"1"` / `"0"` —— 那些键在
        TTL 内仍可能被读到，因此这里必须同时接受两种形态（否则升级期间的审批会被吞掉）。
        """
        if raw is None:
            return None
        text = raw.decode() if isinstance(raw, (bytes, bytearray)) else str(raw)
        text = text.strip()
        if not text:
            return None
        if text[0] == "{":
            try:
                data = json.loads(text)
            except (json.JSONDecodeError, TypeError):
                return None
            if not isinstance(data, dict):
                return None
            decision = str(data.get("decision", "") or "")
            if decision not in VALID_DECISIONS:
                return None
            args = data.get("arguments")
            return cls(
                decision=decision,
                arguments=args if isinstance(args, dict) else None,
                reason=str(data.get("reason", "") or ""),
            )
        lowered = text.lower()
        if lowered in ("1", "true"):
            return cls(decision=DECISION_APPROVE)
        if lowered in ("0", "false"):
            return cls(decision=DECISION_REJECT)
        return None
