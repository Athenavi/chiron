"""prompt_refine — 把用户的**粗略需求**精炼成更明确的提示（差距B）。

对照实现：Reasonix 的 `internal/runtime/promptrefine`。用户常常一句话扔过来
（"帮我弄下那个登录"），而 agent 需要的是一段明确的任务描述。本模块提供**一次**独立的
LLM 改写（复用 `bounded_llm` 的有界调用），由用户在前端显式触发 —— **不自动改写**，
因为"擅自把用户的话换成另一句话"会让人无法预期模型到底收到了什么。

三条边界（写死，不经配置放宽）：

1. **不改变意图**：只做澄清与结构化，绝不添加用户没有暗示的需求；
2. **不回答问题**：它是改写器，不是回答器（否则"精炼"会变成"替你做完"）；
3. **失败不改写**：调用失败 / 输出为空时返回 ``ok=False``，由调用方原样保留用户输入 ——
   精炼失败绝不能把用户的话弄丢。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from app.agent.bounded_llm import call_bounded, clip

logger = logging.getLogger(__name__)

#: 精炼输出的上限：一段提示词不该长到失控。
REFINE_MAX_TOKENS = 1200
#: 输入上限：超长输入只取前若干字符（避免把整篇文档灌进改写请求）。
MAX_INPUT_CHARS = 4000

_SYSTEM_PROMPT = (
    "You rewrite a user's rough request into a clearer, more actionable prompt for a "
    "coding agent.\n"
    "Rules:\n"
    "- Preserve the user's intent and their language EXACTLY. Never translate.\n"
    "- Never add requirements, files, or technologies the user did not mention or clearly imply.\n"
    "- Do NOT answer or execute the request; only rewrite it.\n"
    "- Keep concrete identifiers (names, paths, versions, error messages) verbatim.\n"
    "- Prefer explicit goal + constraints + expected output over vague prose.\n"
    "Output only the rewritten prompt, with no preface or explanation."
)


@dataclass
class RefineResult:
    """一次精炼的结果。失败用 ``ok=False`` 表达，**不改写用户输入**。"""

    refined: str = ""
    ok: bool = False
    error: str = ""

    @property
    def input_tokens(self) -> int:  # 便于调用方统一计量（与其它旁路调用同形）
        return 0


async def refine_prompt(
    gateway: Any,
    *,
    raw: str,
    model: str,
    timeout: float | None = None,
) -> RefineResult:
    """把 ``raw`` 精炼成更明确的提示。

    Args:
        gateway: LLM gateway（``chat(messages, model, max_tokens, temperature)``）。
        raw: 用户原始输入。
        model: 使用的模型（由调用方决定，通常与本轮一致）。
        timeout: 覆盖默认超时（秒）。默认用 `bounded_llm` 的 30s。
    """
    text = (raw or "").strip()
    if not text:
        return RefineResult(ok=False, error="empty prompt")

    messages = [
        {"role": "system", "content": _SYSTEM_PROMPT},
        {"role": "user", "content": clip(text, MAX_INPUT_CHARS)},
    ]
    kwargs: dict[str, Any] = {}
    if timeout is not None:
        kwargs["timeout"] = timeout
    result = await call_bounded(
        gateway,
        messages=messages,
        model=model,
        max_tokens=REFINE_MAX_TOKENS,
        purpose="prompt-refine",
        **kwargs,
    )
    if not result.ok:
        return RefineResult(ok=False, error=result.error)
    refined = result.text.strip()
    if not refined:
        return RefineResult(ok=False, error="refine produced empty output")
    # 防御：模型偶尔会把"改写"做成"回答"，长度暴涨是信号之一。不拦（可能确实需要变长），
    # 但把它限制在输入的一个合理倍数内，避免把一次精炼变成一篇长文。
    if len(refined) > max(MAX_INPUT_CHARS, len(text) * 4):
        refined = refined[: max(MAX_INPUT_CHARS, len(text) * 4)]
    return RefineResult(refined=refined, ok=True)
