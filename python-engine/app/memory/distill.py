"""C6：从回合内容提炼候选 L2 条目（**默认关**）。

**为什么默认关**（评审 01 §2.4）：它是"每回合多一次 LLM 调用"的能力 —— 成本随对话量线性增长，
而收益（自动记住用户偏好）在多数会话里并不明显。默认开等于让所有部署替所有人付这笔钱。
开关交给部署者；开启时用量**必须计入 `task_budget` 的 tokens 轴**，否则预算统计失真、越界判定
失灵（与 S6b 的 grader 同一条理由，见 `app/agent/rubric.py` 的说明）。

**提炼 ≠ 写入**：本模块只产出**候选**；入库由 `MemoryService.ingest_candidates` 负责，且一律
**低置信 + `derived`** —— 它们要能被整理淘汰，也要能供 C4 的冲突裁决介入。自动沉淀的"记忆"
若不比人写的更谨慎，记忆页很快会变成噪声场。
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from typing import Any

from app.memory.layers import SlotType

logger = logging.getLogger(__name__)

#: 单次提炼的 token 上限：只提炼要点，不需要长输出
DEFAULT_MAX_TOKENS = 400
#: 一次提炼最多产出多少条候选（多了就是噪声）
DEFAULT_MAX_ITEMS = 3
#: 候选的**置信上限** —— 自动提炼的条目永远低于用户显式确认（100）
MAX_CANDIDATE_CONFIDENCE = 30

_SLOT_VALUES = {s.value for s in SlotType}

_SYSTEM_PROMPT = (
    "You extract durable user facts from a conversation turn. "
    "Reply with a JSON array ONLY (no commentary, no code fences). "
    "Each item: {\"slot\": one of identity|preference|decision|fact, "
    "\"key\": short snake_case key, \"value\": the fact in the user's language, "
    "\"confidence\": integer 0-100}. "
    "Only include facts worth remembering across sessions (stable preferences, "
    "identities, decisions, long-term facts). Never include transient chatter, "
    "secrets, credentials, or anything about the assistant itself. "
    "If there is nothing worth remembering, reply with []."
)


@dataclass
class Candidate:
    """一条候选记忆（尚未入库）。"""

    slot: str
    key: str
    value: str
    confidence: int


def _strip_fences(raw: str) -> str:
    """剥掉 ```json 围栏 —— 模型很爱加，直接 `json.loads` 会失败。"""
    text = (raw or "").strip()
    if text.startswith("```"):
        text = re.sub(r"^```[a-zA-Z]*\s*", "", text)
        text = re.sub(r"\s*```$", "", text)
    return text.strip()


def parse_candidates(
    raw: str, *, max_items: int = DEFAULT_MAX_ITEMS
) -> list[Candidate]:
    """从模型输出解析候选（容错解析 + 逐条校验）。

    容错是必要的：模型除了围栏还可能前后带解释文字，所以取**第一个 `[` 到最后一个 `]`**。
    但校验**不放宽**：slot 必须是已知枚举、key/value 必须非空 —— 宁可少记，不可乱记。
    """
    text = _strip_fences(raw)
    start, end = text.find("["), text.rfind("]")
    if start < 0 or end <= start:
        return []
    try:
        payload = json.loads(text[start : end + 1])
    except (json.JSONDecodeError, ValueError):
        logger.info("distill: LLM output is not valid JSON, skipping")
        return []
    if not isinstance(payload, list):
        return []

    out: list[Candidate] = []
    for item in payload:
        if not isinstance(item, dict):
            continue
        slot = str(item.get("slot") or "").strip().lower()
        key = str(item.get("key") or "").strip()
        value = str(item.get("value") or "").strip()
        if slot not in _SLOT_VALUES or not key or not value:
            continue
        try:
            confidence = int(item.get("confidence", 0))
        except (TypeError, ValueError):
            confidence = 0
        out.append(
            Candidate(
                slot=slot,
                key=key[:255],  # 与 user_memory_entries.item_key 的列宽一致
                value=value[:2000],
                confidence=max(0, min(MAX_CANDIDATE_CONFIDENCE, confidence)),
            )
        )
        if len(out) >= max_items:
            break
    return out


def _usage(response: Any) -> tuple[int, int]:
    """从模型响应里取 (input_tokens, output_tokens)；取不到记 0（统计宁可少算）。"""
    return (
        int(getattr(response, "input_tokens", 0) or 0),
        int(getattr(response, "output_tokens", 0) or 0),
    )


async def distill_candidates(
    *,
    gateway: Any,
    model: str,
    transcript: str,
    max_items: int = DEFAULT_MAX_ITEMS,
    max_tokens: int = DEFAULT_MAX_TOKENS,
) -> tuple[list[Candidate], int, int]:
    """一次 LLM 调用，产出 (候选列表, input_tokens, output_tokens)。

    失败一律 fail-soft 返回空列表 —— 自动提炼是增强项，不该让它在回合收尾处抛错
    （调用方据此决定"要不要把用量计进预算"：只对真实发生的调用记账）。
    """
    if gateway is None or not (transcript or "").strip():
        return [], 0, 0
    try:
        response = await gateway.chat(
            messages=[
                {"role": "system", "content": _SYSTEM_PROMPT},
                {"role": "user", "content": transcript[:6000]},
            ],
            model=model,
            max_tokens=max_tokens,
        )
    except Exception as exc:  # noqa: BLE001 — 提炼失败不影响回合
        logger.info("distill: LLM call failed: %s", str(exc)[:160])
        return [], 0, 0

    in_tokens, out_tokens = _usage(response)
    candidates = parse_candidates(
        str(getattr(response, "content", "") or ""), max_items=max_items
    )
    return candidates, in_tokens, out_tokens


def render_transcript(
    messages: list[dict[str, Any]], *, max_chars: int = 4000
) -> str:
    """把本回合的消息压成提炼用的文本（只取 user/assistant 的正文）。"""
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
    return text[-max_chars:] if len(text) > max_chars else text
