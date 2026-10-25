"""L3 摘要正文的生成式产出（A3：巩固 pipeline 的第 ① 步）。

旧行为只有 `_extract_summary`：把每条消息的前 200 字符拼起来。它不是"摘要"，而是一
段被截断的原文 —— 于是 §3.7 描述的巩固管道产出的是噪声场，近重复判定（cosine>0.95）
比的是两段互相重叠的原文，主题/实体抽取也从截断过的文本里抽。

**为什么这里可以默认开，而 C6 提炼（`distill.py`）默认关**：提炼是"每回合多一次 LLM
调用"，成本随对话量线性增长；巩固的节拍是"每 `memory_consolidate_batch` 回合一次 +
会话结束一次"（见 `MemoryService.on_turn_complete`），且正文一旦生成就要落库、被后续
召回长期使用 —— 单次调用的收益不按回合数摊。

**租户要传进来**：`GatewayRouter.chat` 的预算预检与扣减都在 `tenant_id` 非空时才发生
（`app/gateway/router.py` 的 `if self._budget and tenant_id`），不传等于让后台记忆任务绕过租户预算。
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    # 只在类型层引用：consolidator 运行时要 import 本模块，反向 import 会成环。
    from app.memory.consolidator import Summariser

logger = logging.getLogger(__name__)

#: 摘要是"几行要点"，输出给多了就是在为噪声付费（与 distill 的 400 同一量级）。
DEFAULT_MAX_TOKENS = 700
#: 输入截断上限。巩固窗口取的是会话消息，长会话不截会每次重送整段历史。
DEFAULT_MAX_INPUT_CHARS = 12000

_SYSTEM_PROMPT = (
    "You consolidate a window of a conversation into durable session memory. "
    "Write a short summary that a different assistant will rely on weeks later. "
    "Keep: decisions actually taken, facts established about the user or the work, "
    "file/URL/identifier strings verbatim, unresolved questions. "
    "Drop: greetings, small talk, restated instructions, and the play-by-play of "
    "tool calls. "
    "Reply with plain prose in the user's own language, 2-6 lines, no preamble, "
    "no markdown fences, no headings. "
    "If the window contains nothing worth remembering, reply with exactly NONE."
)

#: 模型对"没有值得记的内容"的约定回话；命中它就不能把 "NONE" 当成摘要存进去。
_EMPTY_ANSWERS = {"none", "n/a", "no content", "(no content)"}


class NothingWorthRememberingError(Exception):
    """模型明确判定"这一窗没什么值得记"。

    必须与"模型不可用"分开：不可用时回落到抽取式正文是对的（有降级版本总比没有强），
    但模型已经说了"没有值得记的东西"再存一段截断原文，就是把噪声写进以后每回合都要
    召回的长期记忆里 —— 那是 `distill.py` 警告过的"记忆页变噪声场"的同一条路径。
    """


def _is_empty_answer(text: str) -> bool:
    return text.strip().lower().rstrip(".。") in _EMPTY_ANSWERS


def render_conversation(
    messages: list[dict[str, Any]], *, max_chars: int = DEFAULT_MAX_INPUT_CHARS
) -> str:
    """把巩固窗口的消息压成送进模型的文本。

    与 `distill.render_transcript` 同一形状（只取 user/assistant 正文、保留角色标记），
    但**取头部而非尾部**：巩固的是滑出上下文窗口的整段对话，开头往往才是决定与标识符
    所在，而提炼看的是"刚刚这一回合"。
    """
    lines: list[str] = []
    for msg in messages or []:
        role = str(msg.get("role") or "")
        if role not in ("user", "assistant"):
            continue
        content = str(msg.get("content") or "").strip()
        if not content:
            continue
        lines.append(f"[{role}] {content}")
    text = "\n".join(lines)
    return text[:max_chars] if len(text) > max_chars else text


async def summarise_messages(
    messages: list[dict[str, Any]],
    *,
    gateway: Any,
    model: str,
    tenant_id: int = 0,
    max_input_chars: int = DEFAULT_MAX_INPUT_CHARS,
    max_tokens: int = DEFAULT_MAX_TOKENS,
) -> str:
    """一次 LLM 调用产出摘要正文。

    返回空串 = **这次没做成**（没配网关、正文为空、调用抛错、网关回 `finish_reason="error"`）。
    调用方据此回落到抽取式正文：巩固是后台链路，"这次没总结好"远不到让任务失败的份量 ——
    正文有降级版本仍然可检索，而抛错会让整条记忆写入进重试直至 DLQ。

    抛 :class:`NothingWorthRememberingError` = 模型做了判定，结论是"没什么值得记"。
    """
    if gateway is None:
        return ""
    transcript = render_conversation(messages, max_chars=max_input_chars)
    if not transcript.strip():
        return ""
    try:
        response = await gateway.chat(
            messages=[
                {"role": "system", "content": _SYSTEM_PROMPT},
                {"role": "user", "content": transcript},
            ],
            model=model,
            tenant_id=tenant_id,
            max_tokens=max_tokens,
            temperature=0.1,
        )
    except Exception as exc:  # noqa: BLE001 — 摘要失败退回抽取式，不影响写入
        logger.info("summariser: LLM call failed: %s", str(exc)[:160])
        return ""

    # 网关（app/gateway/router.py）的失败形状是 ChatResponse(finish_reason="error")，不抛异常。
    if getattr(response, "finish_reason", "") == "error":
        logger.info(
            "summariser: gateway returned error: %s",
            str(getattr(response, "message", "") or "")[:160],
        )
        return ""

    content = str(getattr(response, "content", "") or "").strip()
    if not content:
        return ""
    if _is_empty_answer(content):
        raise NothingWorthRememberingError("模型判定该巩固窗口没有值得长期记忆的内容")
    return content


def build_summariser(
    gateway: Any,
    *,
    model: str = "",
    tenant_id: int = 0,
    max_input_chars: int = DEFAULT_MAX_INPUT_CHARS,
    max_tokens: int = DEFAULT_MAX_TOKENS,
) -> Summariser:
    """装配成一个 `Consolidator` 认识的 Summariser（`Callable[[messages], Awaitable[str]]`）。

    配置在这里收口一次而不是散到各装配点：`Consolidator` 由 app/main.py 建一个实例，
    worker 经 `MemoryService.save_summary` 复用它，所以开关、模型、上限三处口径天然同源。

    `tenant_id` 参数只是闭包的兜底值 —— 真正的租户由 `Consolidator.consolidate` 在每次
    调用时传进来（预算扣减必须按发生那次巩固的租户记，装配时还不知道）。
    """
    from app.config import settings

    # 三级兜底而不是让装配点各写一遍：显式传入 > `memory_summariser_model`（空 = 复用
    # `default_model`，与 `guardian_model` 同口径）。写在调用方会让这条配置随装配点漂移。
    use_model = model or settings.memory_summariser_model or settings.default_model

    async def _summarise(messages: list[dict[str, Any]], tid: int = 0) -> str:
        if not settings.memory_summariser_enabled:
            return ""
        return await summarise_messages(
            messages,
            gateway=gateway,
            model=use_model,
            tenant_id=tid or tenant_id,
            max_input_chars=int(settings.memory_summariser_max_input_chars),
            max_tokens=int(settings.memory_summariser_max_tokens),
        )

    return _summarise
