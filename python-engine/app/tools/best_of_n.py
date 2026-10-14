"""best_of_n 工具 —— 多候选 + judge 选优（差距D）。

模型用它把"一个答案"变成"几个并排的答案"，再由独立 judge 选优。返回里**总是**带上
全部候选（即便选出了 winner），这样用户能看到别的路 —— 只回 winner 会让"选优"变成黑箱。

判据（`criteria`）由调用方给；不给时用 `app/agent/best_of_n.py` 的默认序
（正确性 > 清晰度 > 简洁）。
"""

from __future__ import annotations

import logging
from typing import Any

from app.tools.registry import registry

logger = logging.getLogger(__name__)

BEST_OF_N_TOOL = "best_of_n"


async def best_of_n(
    task: str,
    candidates: list[dict[str, Any]],
    criteria: str = "",
) -> dict[str, Any]:
    """在多个候选方案里选出一个最好的。

    Args:
        task: 这些候选要解决的**同一个**任务（judge 按它判断）。
        candidates: 候选项列表，每项形如 ``{"label": "A", "content": "..."}``。
            `label` 可省略（自动 A/B/C…）。2–6 个。
        criteria: 选优判据（可省略，省略时用默认序）。
    """
    from app.agent.best_of_n import judge_candidates, parse_candidates

    parsed, errors = parse_candidates(candidates)
    if errors:
        # 整份拒绝：半份候选会让"选优"失去意义（可能漏掉真正最好的那个）。
        return {"error": "invalid candidates: " + "; ".join(errors)}

    from app.config import settings
    from app.tools.context import get_gateway

    gateway = get_gateway()
    if gateway is None:
        return {"error": "no LLM gateway available for judging"}

    judgment = await judge_candidates(
        gateway,
        task=str(task or ""),
        candidates=parsed,
        model=settings.default_model,
        criteria=criteria,
    )
    if not judgment.ok:
        # 选不出就**不选** —— 把全部候选原样交回给调用方/用户，而不是随便挑一个。
        return {
            "error": f"judge failed: {judgment.error}",
            "candidates": [{"label": c.label, "content": c.content} for c in parsed],
        }

    winner = next((c for c in parsed if c.label == judgment.winner), None)
    return {
        "winner": judgment.winner,
        "content": winner.content if winner else "",
        "scores": judgment.scores,
        "rationale": judgment.rationale,
        "count": len(parsed),
        # 全部候选一并返回：用户要能看到被放弃的那几条路。
        "candidates": [{"label": c.label, "content": c.content} for c in parsed],
    }


registry.register(
    name=BEST_OF_N_TOOL,
    description=(
        "Compare several candidate answers to the SAME task and let an independent judge pick "
        "the best one. Use it when a problem has genuinely different approaches worth comparing "
        "(refactors, bug fixes, design trade-offs): pass 2-6 candidates and the shared task, and "
        "you get the winner plus every candidate's score and rationale. Prefer it over committing "
        "to your first idea when the choice matters."
    ),
    parameters={
        "type": "object",
        "properties": {
            "task": {"type": "string", "description": "The single task all candidates must solve"},
            "criteria": {
                "type": "string",
                "description": "Optional judging criteria (defaults to correctness > clarity > brevity)",
            },
            "candidates": {
                "type": "array",
                "description": "2-6 candidates: [{\"label\": \"A\", \"content\": \"...\"}]",
                "items": {
                    "type": "object",
                    "properties": {
                        "label": {"type": "string", "description": "Optional short label"},
                        "content": {"type": "string", "description": "The candidate answer"},
                    },
                    "required": ["content"],
                },
            },
        },
        "required": ["task", "candidates"],
    },
    handler=best_of_n,
)
