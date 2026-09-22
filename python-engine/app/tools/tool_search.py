"""tool_search — 按需激活工具（Token Economy 的入口）。

## 为什么需要它

每个模式默认只暴露少量"常用工具"（常规/创造 12 个、极简 3 个），
其余 50+ 个（`browser_*` / `image_generate` / `workflow_run` / `graph_run` /
`speech_to_text` / `text_to_speech` / `vision_analyze` / `agent_session_*` …）
**并不在工具列表里**。这是有意的取舍：工具 schema 很占 token，全塞给模型既贵、
又会降低选择准确率（`modes.py` 顶部那句"只暴露这些给 LLM，其余按需激活"）。

但"看不见"不等于"用不了"。本工具就是那个入口：LLM 用自然语言（或关键词）描述
它想做的事，这里在**全量注册表**里检索，命中后把工具**加入本会话的已激活集合**，
**下一轮**它就会出现在工具列表里，于是可以正常调用。

## 三个关键设计

1. **下一轮才生效** —— OpenAI function-calling 协议下，模型只能调用"本轮 tools
   里声明过的"工具。所以"本轮搜索、下一轮使用"是唯一可行的时序。返回值里明确
   写了这一点，免得模型本轮就去调用刚搜到的工具然后失败。
2. **会话级持久** —— 激活集合挂在 AgentRuntime 实例上（见 `runtime.run` 里的
   `set_tool_context(activated_tools=...)`），不是每轮重建，否则搜完立刻丢。
3. **不泄漏跨用户工具** —— 复用 `registry.to_openai_tools()`，它已按当前用户过滤
   owner 工具（MCP/插件注入的工具不会串号）。

搜索本身**只读、无副作用**：它不执行任何被搜到的工具。
"""

from __future__ import annotations

import re
from typing import Any

from app.tools.registry import registry

TOOL_SEARCH_NAME = "tool_search"

#: 中文/口语关键词 → 工具名片段。模型常直接用中文描述意图（"帮我截个图"），
#: 而工具名与描述大多是英文，纯英文匹配会漏掉这些查询。
_ALIASES: dict[str, str] = {
    "浏览器": "browser",
    "网页": "browser",
    "打开网站": "browser",
    "截图": "screenshot",
    "抓取": "browser",
    "图像": "image",
    "画图": "image",
    "生成图": "image_generate",
    "看图": "vision",
    "识图": "vision",
    "语音": "speech",
    "听写": "speech_to_text",
    "转写": "speech_to_text",
    "朗读": "text_to_speech",
    "配音": "text_to_speech",
    "工作流": "workflow",
    "编排": "workflow",
    "流程图": "graph",
    "子agent": "subagent",
    "子代理": "subagent",
    "委派": "agent_dispatch",
    "后台": "run_in_background",
    "定时": "job",
    "任务状态": "job_output",
    "知识库": "kb",
    "检索": "rag_query",
    "文档": "file_analyzer",
    "分析文件": "file_analyzer",
    "需求": "prd_generate",
    "技术方案": "tech_design",
    "拆解": "task_decompose",
    "记忆": "memory_search",
}


def _expand_terms(query: str) -> list[str]:
    """把查询切成词，并把中文关键词映射为工具名片段。"""
    terms = [w for w in re.split(r"[\s,，、/|:：]+", query) if w]
    expanded = list(terms)
    lowered = query.lower()
    for cn, tool in _ALIASES.items():
        if cn in lowered and tool not in expanded:
            expanded.append(tool)
    return expanded


def _score(terms: list[str], name: str, desc: str) -> int:
    """命中打分：工具名命中权重高于描述命中。空查询返回 1（列出全部）。"""
    if not terms:
        return 1
    lname = name.lower()
    ldesc = desc.lower()
    score = 0
    for w in terms:
        w = w.strip().lower()
        if not w:
            continue
        if w == lname:
            score += 10
        elif w in lname:
            score += 4
        elif w in ldesc:
            score += 1
    return score


async def tool_search(query: str = "", limit: int = 8) -> dict[str, Any]:
    """按需发现并激活工具。

    Args:
        query: 自然语言或关键词（支持中文，如 "浏览器" / "生成图片" / "跑工作流"）。
            留空则列出**全部**可用工具，便于模型了解还有什么。
        limit: 最多激活多少个（默认 8，上限 20）。
    """
    from app.tools.context import activate_tools, get_activated_tools

    limit = max(1, min(int(limit or 8), 20))
    terms = _expand_terms((query or "").strip())

    visible = registry.to_openai_tools()  # 已按当前用户过滤
    already = set(get_activated_tools())

    scored: list[tuple[int, str, str]] = []
    for t in visible:
        fn = t.get("function") or {}
        name = fn.get("name") or ""
        if not name or name == TOOL_SEARCH_NAME:
            continue
        desc = fn.get("description") or ""
        s = _score(terms, name, desc)
        if s > 0:
            scored.append((s, name, desc))

    scored.sort(key=lambda x: (-x[0], x[1]))
    picked = scored[:limit]
    names = [n for _, n, _ in picked]
    if names:
        activate_tools(names)

    hits = [{"name": n, "description": d[:160]} for _, n, d in picked]
    if not names:
        note = (
            "没有匹配的工具。可以只写关键词再试一次，"
            "例如：浏览器 / 图像 / 语音 / 工作流 / 子agent / 知识库 / 记忆。"
        )
    else:
        note = (
            "以下工具已加入本会话的激活列表，**从下一轮开始可调用**。"
            "OpenAI function-calling 只允许调用本轮已声明的工具，"
            "所以**本轮不要直接调用它们** —— 请先把发现告知用户，下一轮再发起调用。"
        )
    return {
        "activated": names,
        "matches": hits,
        "already_active": sorted(already),
        "total_available": len(visible),
        "note": note,
    }


registry.register(
    name=TOOL_SEARCH_NAME,
    description=(
        "Discover and activate additional tools on demand. Only a small core set of "
        "tools is exposed by default; the rest (browser automation, image generation, "
        "vision, speech, workflows, graphs, background jobs, knowledge base, …) must be "
        "found through this tool first. Pass a natural-language query or keywords "
        "(Chinese is supported, e.g. '浏览器', '生成图片', '跑工作流'), or leave it empty "
        "to list everything available. Matched tools become callable from the NEXT turn."
    ),
    parameters={
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": "What you want to do, e.g. 'take a screenshot of a page', '浏览器', '生成图片'. Empty = list all.",
            },
            "limit": {
                "type": "integer",
                "description": "Max tools to activate (default 8, max 20)",
                "default": 8,
            },
        },
        "required": [],
    },
    handler=tool_search,
)
