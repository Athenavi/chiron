"""子 Agent 生命周期遥测（A5）—— **content-free** 的可选审计通道。

对齐的参照是 Reasonix 的 `SubagentLifecycleInfo`，它的取舍值得照抄：这类信息**刻意不含**
prompt / 推理 / 工具输出 / 路径。理由是这类遥测要能**转发进诊断链路**（日志聚合、审计 sink、
外部系统），一旦掺了内容，转发就等于泄漏会话内容。

## 与 `app/agent/event_sink.py` 的分工

`event_sink` 是**面向渲染**的（8 种事件，带 reasoning/text 内容，受每秒预算约束）；
本模块是**面向诊断**的：

| | `event_sink` | 本模块 |
|---|---|---|
| 用途 | 前端实时预览 | 诊断 / 审计 |
| 内容 | **有**（思考、正文、工具参数） | **无** |
| 消费方 | 必须（前端） | **可选 opt-in** |
| 丢失后果 | 界面不更新 | 少一条遥测 |

混在一个通道里会逼出两难：既想转发诊断，就得连内容一起转。所以分开。

## `content-free` 是**类型级**保证，不是约定

`SubagentLifecycle` **没有**任何承载内容的字段 —— 不存在"忘了过滤"的可能性，因为压根没有可
承载的位置。`tests/test_subagent_lifecycle.py` 用 `dataclasses.fields` 把这条钉成断言
（字段名里出现 content / prompt / text / output_msg / path 之类即失败），这样以后有人想"顺手
加个 error_text"时会**在测试里**被拦下。
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Protocol

#: Reasonix 的 7 个阶段（`internal/event/subagent_lifecycle.go`）。用**元组**而不是枚举：
#: 这一步只需"取值受限 + 常量可 import"，不值得引入一组类。
PHASE_CREATED = "child_created"
PHASE_RUNNING = "child_running"
PHASE_COMPLETED = "child_completed"
PHASE_PARTIAL = "child_partial"
PHASE_FAILED = "child_failed"
PHASE_CANCELLED = "child_cancelled"
PHASE_RESUME = "child_resume"

PHASES: tuple[str, ...] = (
    PHASE_CREATED,
    PHASE_RUNNING,
    PHASE_COMPLETED,
    PHASE_PARTIAL,
    PHASE_FAILED,
    PHASE_CANCELLED,
    PHASE_RESUME,
)

#: 这些字段名一律不许出现在遥测里（测试会断言）。放成常量是为了让"禁用名单"本身可被引用 ——
#: 将来若要加字段，先来这里看是不是踩线。
FORBIDDEN_FIELD_HINTS: tuple[str, ...] = (
    "content",
    "prompt",
    "text",
    "message",
    "output",
    "summary",
    "path",
    "arg",
    "reasoning",
    "thinking",
    "transcript",
)

#: **显式豁免**：命中禁用词、但语义上不是内容。
#:
#: `output_bytes` 是**体量**（一个整数），不是"输出内容" —— 它恰恰就是"不含内容却仍能描述产出"
#: 的做法本身，是 A5 想要的东西而不是要拦的东西。
#:
#: 为什么用豁免名单、而不是把 `output` 从禁用词里拿掉：要拦的正是 `output_text` / `output_msg`
#: 这类名字。豁免必须**一个一个点名**，这样每次新增豁免都是一次显式决定。
ALLOWED_FIELD_NAMES: frozenset[str] = frozenset({"output_bytes"})


@dataclass(frozen=True)
class SubagentLifecycle:
    """一次生命周期转移的**无内容**描述。

    `error_code` 是**码**（如 ``budget_exceeded`` / ``timeout``），**不是**错误文本 ——
    文本里常带路径与片段，那正是本模块要避开的东西。
    """

    phase: str
    run_id: str
    parent_run_id: str = ""
    depth: int = 1
    profile: str = ""
    model: str = ""
    status: str = ""
    error_code: str = ""
    retryable: bool = False
    output_bytes: int = 0
    duration_ms: int = 0
    validator_mode: str = ""
    validator_outcome: str = ""
    validator_attempt: int = 0
    provider_request_id: str = ""
    extra: dict[str, Any] = field(default_factory=dict)

    def to_payload(self) -> dict[str, Any]:
        payload = asdict(self)
        # `extra` 是**结构化**附加项（计数、布尔、枚举），不是逃生舱：内容仍然放不进来
        payload.pop("extra", None)
        payload.update(self.extra or {})
        return payload


class LifecycleSink(Protocol):
    """消费方接口。实现方**不该**在这里做重活（它是旁路）。"""

    def emit(self, info: SubagentLifecycle) -> None: ...


_sink: LifecycleSink | None = None


def register_lifecycle_sink(sink: LifecycleSink | None) -> None:
    """显式 opt-in。传 ``None`` 等于注销。"""
    global _sink
    _sink = sink


def get_lifecycle_sink() -> LifecycleSink | None:
    return _sink


def reset_lifecycle_sink() -> None:
    """测试用：回到"没有 sink"的默认态。"""
    register_lifecycle_sink(None)


def emit_lifecycle(info: SubagentLifecycle) -> None:
    """发一条遥测。**没有 sink 时零开销；sink 抛异常也不影响委派。**

    这两条合起来才是"可选通道"：消费方没接时不该有成本，接了但出错时不该把子 Agent 带崩。
    """
    sink = _sink
    if sink is None:
        return
    try:
        sink.emit(info)
    except Exception:  # noqa: BLE001 — 旁路失败绝不影响主流程
        pass


__all__ = [
    "ALLOWED_FIELD_NAMES",
    "FORBIDDEN_FIELD_HINTS",
    "PHASES",
    "PHASE_CANCELLED",
    "PHASE_COMPLETED",
    "PHASE_CREATED",
    "PHASE_FAILED",
    "PHASE_PARTIAL",
    "PHASE_RESUME",
    "PHASE_RUNNING",
    "LifecycleSink",
    "SubagentLifecycle",
    "emit_lifecycle",
    "get_lifecycle_sink",
    "register_lifecycle_sink",
    "reset_lifecycle_sink",
]
