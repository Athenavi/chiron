"""分支压缩：把会话"保留区"的历史压成一份能继续工作的核心上下文。

为什么不复用 ``app.context.manager.ContextManager.compress``
--------------------------------------------------------
1. `compress` 的触发条件是"**超过 token 阈值**"；分支是**显式要求压缩** ——
   哪怕不算长，也要压出"绝对核心"（用户诉求第 2 条）；
2. `compress` 的提示词目标是"把旧消息压短"，分支的目标是"**保留**目标/约束/结论/待办，
   并且关键标识符逐字保留"，两者提示词不同；
3. 分支的产出要**结构化**（人可读、后续模型可稳定消费）并带 degraded 与 usage。

为什么本模块**不写数据库**
----------------------
`messages` 落库、turn 状态、SSE 推送都在 Go（见 ``app/queue/worker.py`` 中
``_handle_agent_followup`` 的注释："``messages`` 落库、SSE 推送、turn 状态、计费与会话锁
全在 Go"）。引擎只负责"调模型 → 返回摘要"，由 Go 决定把摘要写成哪条消息、把
``branch_state`` 置成什么。这样职责单一，也避免两端同时写同一张表。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger(__name__)

# 压缩区少于这么多条就不值得调模型（与 Go 侧 minCondenseMessages 对齐）
MIN_COMPRESSIBLE_MESSAGES = 3
# 默认保留最近 4 条原文（与 Go 侧 defaultBranchKeepTail 对齐）
DEFAULT_KEEP_TAIL = 4
# 摘要字符上限：防止模型"复述全文"
SUMMARY_MAX_CHARS = 4000
# 送给模型的字符预算：超出时**保首尾、丢中段**（首=目标，尾=最近的进展）
PROMPT_MAX_CHARS = 24000
LLM_MAX_TOKENS = 1500

BRANCH_SUMMARY_PROMPT = """你是"会话裁剪压缩器"。下面给出一段对话历史（时间上更早的部分；
紧接着的最近几轮原文会被单独保留，不在你这里）。

请把它压缩成一份**能让人与模型无缝继续工作的核心上下文**：

必须逐字保留（不得改写、不得翻译）：
- 关键标识符：文件路径、函数/类名、命令、参数、错误码、ID、URL、配置项名
- 用户明确的目标、约束、禁忌与验收标准

必须保留（可用简短句）：
- 已经达成的事实与结论（改了什么、跑通了什么、指标是多少、有什么证据）
- 当前未解决的问题、失败尝试及其原因、下一步

可以丢弃：
- 寒暄与重复确认、探索过程的中间叙述、工具调用的原始输出（只留结论）

输出 Markdown，四段，某段为空则省略：

## 目标
## 已完成
## 关键事实
## 待办

只输出这份摘要本身，不要解释你在做什么。"""

FUTURE_PROMPT_SUFFIX = """

另外：这段历史**之后**还发生过一些对话（附在末尾，用 <后续> 包起来）。
请只额外补一句"后续走向"（≤80 字），用 `## 后续走向` 起一段 —— 不要把它混进上面四段。"""


@dataclass
class CondenseResult:
    """一次分支压缩的结果。

    ``condensed=False`` 表示**没有可压缩的内容**（保留区太短）——
    调用方据此把 ``branch_state`` 直接置 ``ready``，而不是显示"压缩中"。
    """

    summary: str = ""
    condensed: bool = False
    degraded: bool = False
    reason: str = ""
    source_messages: int = 0
    usage: dict[str, Any] = field(default_factory=dict)


def split_head_tail(
    messages: list[dict[str, Any]], keep_tail: int
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """按"最近 K 条留原文"切分，返回 ``(压缩区, 原文保留区)``。

    边界：``keep_tail <= 0`` 视为"全部压缩"（没有原文保留区）；
    ``keep_tail >= len(messages)`` 视为"全部留原文"（没有可压缩的内容）。
    """
    items = list(messages or [])
    if keep_tail <= 0:
        return items, []
    if keep_tail >= len(items):
        return [], items
    return items[:-keep_tail], items[-keep_tail:]


def render_messages(
    messages: list[dict[str, Any]], limit_chars: int = PROMPT_MAX_CHARS
) -> str:
    """把消息渲染成提示词文本；超预算时保首尾（目标在最前、进展在最后）。"""
    lines: list[str] = []
    for msg in messages or []:
        content = msg.get("content")
        if not isinstance(content, str) or not content.strip():
            continue
        lines.append("[{}] {}".format(msg.get("role", "user"), content.strip()))
    text = "\n".join(lines)
    if len(text) <= limit_chars:
        return text
    half = max(1, limit_chars // 2)
    return text[:half] + "\n…（中段省略）…\n" + text[-half:]


def extractive_summary(
    messages: list[dict[str, Any]], max_chars: int = SUMMARY_MAX_CHARS
) -> str:
    """LLM 不可用时的兜底：取每条消息首行拼成要点清单。

    为什么一定要有兜底：压缩失败不该让分支"什么都得不到" —— 提取式摘要至少保住
    "每轮在谈什么"，且调用方据此把状态置成 degraded 而不是 failed。
    """
    parts: list[str] = []
    for msg in messages or []:
        content = msg.get("content")
        if not isinstance(content, str) or not content.strip():
            continue
        first_line = content.strip().splitlines()[0][:200]
        parts.append("- [{}] {}".format(msg.get("role", "user"), first_line))
    body = "\n".join(parts) if parts else "- （无可提取内容）"
    return ("## 已完成（提取式降级摘要：模型不可用，按原话首行保留）\n" + body)[:max_chars]


def _usage_of(resp: Any) -> dict[str, Any]:
    """尽最大努力读出 usage（不同 provider 字段名不同，读不到就返回空）。"""
    usage = getattr(resp, "usage", None)
    if usage is None:
        return {}
    out: dict[str, Any] = {}
    for key in ("prompt_tokens", "completion_tokens", "total_tokens"):
        value = getattr(usage, key, None)
        if isinstance(value, int):
            out[key] = value
    return out


async def condense_messages(
    *,
    messages: list[dict[str, Any]],
    gateway: Any = None,
    keep_tail: int = DEFAULT_KEEP_TAIL,
    include_future: bool = False,
    future_messages: list[dict[str, Any]] | None = None,
) -> CondenseResult:
    """把 ``messages`` 的**压缩区**压成核心上下文摘要。

    Args:
        messages: 源会话的消息（``role``/``content``），时间升序。
        gateway: LLM gateway；为 None 时直接走提取式降级。
        keep_tail: 最近多少条**留原文**（不进压缩区）。
        include_future: 是否把"被裁掉的后段"也压成一句后续走向（默认否 —— 否则
            新会话会"记得未来"，语义混乱，见 docs/session-map-branch-design.md 2.3）。
        future_messages: ``include_future`` 为真时提供的后段消息。

    Returns:
        CondenseResult：``condensed=False`` 表示无可压缩内容（保留区太短）。
    """
    head, _tail = split_head_tail(messages, keep_tail)
    result = CondenseResult(source_messages=len(head))
    if len(head) < MIN_COMPRESSIBLE_MESSAGES:
        result.reason = "compressible_too_small"
        return result
    result.condensed = True

    summary = ""
    use_future = bool(include_future and future_messages)
    if gateway is not None:
        system_prompt = BRANCH_SUMMARY_PROMPT + (FUTURE_PROMPT_SUFFIX if use_future else "")
        user_prompt = render_messages(head)
        if use_future:
            user_prompt += (
                "\n\n<后续>\n"
                + render_messages(future_messages or [])
                + "\n</后续>"
            )
        try:
            from app.config import settings

            resp = await gateway.chat(
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_prompt},
                ],
                model=settings.default_model,
                max_tokens=LLM_MAX_TOKENS,
            )
            summary = (getattr(resp, "content", "") or "").strip()
            result.usage = _usage_of(resp)
        except Exception as exc:  # noqa: BLE001 - 任何 provider 异常都降级，分支不能因此失败
            logger.warning("branch condense LLM failed, falling back to extractive: %s", exc)
            summary = ""

    if not summary:
        summary = extractive_summary(head)
        result.degraded = True

    result.summary = summary[:SUMMARY_MAX_CHARS]
    return result
