"""skill_catalog — 持久技能目录注入（对应 deepseek dsh-tool-skill 的 catalog）

在 agent 消息开头注入一条携带 `<available_skills>` 摘要的 user 消息
（仅 name+description，不注入正文），让模型跨轮次感知可用技能而不必
主动调用 skill_list；会话内已存在目录时跳过（保持前缀稳定）。
"""

from __future__ import annotations

import logging
from typing import Any

from app.tools.registry import registry

logger = logging.getLogger(__name__)

CATALOG_MARKER = "<available_skills>"


async def build_skill_catalog() -> str:
    """返回技能目录文本（无技能时返回空串）。"""
    tool = registry.get("skill_list")
    if tool is None:
        return ""
    try:
        result = await tool.handler()
    except Exception as e:  # noqa: BLE001
        logger.debug("skill catalog build failed: %s", e)
        return ""
    skills = result.get("skills") or []
    # 停用技能不进 agent 目录
    skills = [s for s in skills if s.get("enabled", True)]
    if not skills:
        return ""
    lines = [
        "<available_skills>",
        "You have these skills installed. Load one only when needed; do not re-load already-inlined content.",
    ]
    for s in skills:
        name = s.get("name", "")
        desc = s.get("description", "")
        lines.append(f"- {name}: {desc}")
    lines.append("</available_skills>")
    return "\n".join(lines)


async def inject_skill_catalog(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """若消息中尚无技能目录且存在技能，则注入一条 user 消息。"""
    for m in messages:
        if CATALOG_MARKER in (m.get("content", "") or ""):
            return messages  # 已有目录，保持前缀稳定
    catalog = await build_skill_catalog()
    if not catalog:
        return messages
    return [{"role": "user", "content": catalog}] + messages
