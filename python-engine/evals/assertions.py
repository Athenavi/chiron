"""断言引擎（E1）—— **success 硬失败 / efficiency 只记录**。

`Observation` 由 runner 从「SSE 事件 + 运行后产物」收集；本模块只做**纯函数判定**，
不碰网络与文件系统，因此可以被完整单测（评测骨架自身也需要回归网）。

对位 deepagents 的双层断言模型（`libs/evals/CONTRIBUTING.md:32-49`）：success 断言不满足
即任务失败；efficiency 断言（步数/工具数/token/耗时）**只记录**，用来发现"做对了但绕远路"
的回归，而不会把有效解判成失败。
"""

from __future__ import annotations

from dataclasses import dataclass, field

from evals.firmware import Assertion, Efficiency


@dataclass
class Observation:
    """一次运行的观测结果（runner 负责填充）。"""

    #: SSE 事件（`{"type": ..., "content": ...}`）
    events: list[dict] = field(default_factory=list)
    #: 最终回答文本（`done` 之前累积的 text 事件）
    final_text: str = ""
    #: 运行后从 workspace 读回的产物文件（相对路径 → 内容）
    files: dict[str, str] = field(default_factory=dict)
    #: 工具调用（`{"name": ..., "args": ..., "error": str | None}`）
    tool_calls: list[dict] = field(default_factory=list)
    #: 助手回合数（runner 按 `tool_call` 批次或 text 分段估算）
    steps: int = 0
    #: 总 token（`done` 事件的 input+output）
    tokens: int = 0
    #: 墙钟耗时（毫秒）
    wall_ms: int = 0

    def event_types(self) -> list[str]:
        return [str(e.get("type", "")) for e in self.events]

    def tool_names(self) -> list[str]:
        return [str(c.get("name", "")) for c in self.tool_calls]


@dataclass(frozen=True)
class Check:
    """一条 success 断言的判定结果。"""

    kind: str
    ok: bool
    detail: str = ""


@dataclass
class Evaluation:
    """一条任务的完整判定。"""

    passed: bool
    success_checks: list[Check]
    efficiency_notes: list[str]

    @property
    def failures(self) -> list[Check]:
        return [c for c in self.success_checks if not c.ok]


def _check(assertion: Assertion, obs: Observation) -> Check:
    kind = assertion.kind

    if kind == "final_text_contains":
        return Check(kind, assertion.value in obs.final_text, f"期望最终回答含 {assertion.value!r}")

    if kind == "final_text_not_contains":
        return Check(
            kind, assertion.value not in obs.final_text, f"期望最终回答不含 {assertion.value!r}"
        )

    if kind == "file_contains":
        content = obs.files.get(assertion.path)
        ok = content is not None and assertion.value in content
        return Check(kind, ok, f"{assertion.path} 应含 {assertion.value!r}")

    if kind == "file_not_contains":
        # 与 `file_contains` 对称：用于"改完了、旧值不该还在"这类断言（E1 的 full 集需要）。
        # 文件**不存在**也算不含 —— 这里问的是"有没有这个内容"，不是"文件在不在"。
        content = obs.files.get(assertion.path) or ""
        return Check(kind, assertion.value not in content, f"{assertion.path} 不应含 {assertion.value!r}")

    if kind == "file_equals":
        content = obs.files.get(assertion.path)
        return Check(kind, content == assertion.value, f"{assertion.path} 应等于期望内容")

    if kind == "tool_called":
        return Check(kind, assertion.tool in obs.tool_names(), f"应调用过工具 {assertion.tool!r}")

    if kind == "tool_denied":
        # 被拒 = 该工具**被调用过**且结果带 error（准入检查/审批拒绝都算）
        denied = [
            c for c in obs.tool_calls if c.get("name") == assertion.tool and c.get("error")
        ]
        return Check(kind, bool(denied), f"工具 {assertion.tool!r} 应被拒绝")

    if kind == "event_emitted":
        return Check(
            kind, assertion.value in obs.event_types(), f"应发出 {assertion.value!r} 事件"
        )

    if kind == "guardrail_blocked":
        return Check(kind, "guardrail_blocked" in obs.event_types(), "应被护栏拦下")

    return Check(kind, False, f"未实现的断言类别：{kind!r}")


def _efficiency_notes(eff: Efficiency, obs: Observation) -> list[str]:
    """效率期望**只记录**：超出即写一条 note，**不改变** `passed`。"""
    notes: list[str] = []
    if eff.max_steps is not None and obs.steps > eff.max_steps:
        notes.append(f"steps {obs.steps} > 期望 {eff.max_steps}")
    if eff.max_tool_calls is not None and len(obs.tool_calls) > eff.max_tool_calls:
        notes.append(f"tool_calls {len(obs.tool_calls)} > 期望 {eff.max_tool_calls}")
    if eff.max_tokens is not None and obs.tokens > eff.max_tokens:
        notes.append(f"tokens {obs.tokens} > 期望 {eff.max_tokens}")
    if eff.max_wall_ms is not None and obs.wall_ms > eff.max_wall_ms:
        notes.append(f"wall_ms {obs.wall_ms} > 期望 {eff.max_wall_ms}")
    return notes


def evaluate(
    assertions: tuple[Assertion, ...], efficiency: Efficiency, obs: Observation
) -> Evaluation:
    """判定一次运行：success 全过才 `passed`；efficiency 只产出 notes。"""
    checks = [_check(a, obs) for a in assertions]
    return Evaluation(
        passed=all(c.ok for c in checks),
        success_checks=checks,
        efficiency_notes=_efficiency_notes(efficiency, obs),
    )
