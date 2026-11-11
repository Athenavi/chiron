"""工作台上下文（workbench context）的读取与合并口径。

前端 `WorkbenchQuickStart` / `ChatSidePanel` 允许多选（`?kb=a&kb=b&...`），组装成
`kb_ids` / `agent_ids` 数组。本模块把这套读取收敛成一处，
避免三条链路（SSE submit / chat submit / quick-execute）各写一遍"先看复数、再回退
单数"的逻辑而出现行为分歧 —— 那是"在首页多选生效、在侧栏多选不生效"这类
最难查的 bug 的根源。
"""

from __future__ import annotations

from typing import Any


def _as_str_list(raw: Any) -> list[str]:
    """把任意形态的字段收敛成去空、去重、保序的字符串列表。"""
    if isinstance(raw, str):
        value = raw.strip()
        return [value] if value else []
    if isinstance(raw, (list, tuple)):
        out: list[str] = []
        for item in raw:
            if not isinstance(item, str):
                continue
            value = item.strip()
            if value and value not in out:
                out.append(value)
        return out
    return []


def context_ids(workbench_context: Any, name: str) -> list[str]:
    """读取多值字段 ``<name>_ids``，缺失时回退单值 ``<name>_id``。

    单值回退是**向后兼容**：老前端与手写 URL（``?kb=xxx``）只发单值。回退只在
    复数完全缺失时发生，不合并两者 —— 两者同时出现时以复数（更新的契约）为准。
    """
    ctx = workbench_context if isinstance(workbench_context, dict) else {}
    ids = _as_str_list(ctx.get(f"{name}_ids"))
    if ids:
        return ids
    return _as_str_list(ctx.get(f"{name}_id"))


def merge_by_quota[T](groups: list[list[T]], limit: int) -> list[T]:
    """多来源检索结果的轮询配额合并。

    多选知识库的意图是"这几个库都要用"，所以每轮从各来源取一条（保证每个来源都有
    代表），而不是把各来源结果按分数混排后截断 —— 后者会让高分来源占满配额、低分
    来源形同没选。何况不同知识库的 embedding 与索引不同，**分数本就不可比**，
    混排排序本身就没有意义。

    ``limit`` 是合并后的硬上限（保护首字节延迟）；``limit <= 0`` 返回空。
    """
    if limit <= 0:
        return []
    merged: list[T] = []
    index = 0
    while len(merged) < limit:
        progressed = False
        for group in groups:
            if index < len(group):
                merged.append(group[index])
                progressed = True
                if len(merged) >= limit:
                    break
        if not progressed:  # 所有来源都已取空
            break
        index += 1
    return merged


def selected_memory_slots(workbench_context: Any) -> list[str]:
    """本次对话要注入的长期记忆分类（``memory_slots``）。

    空列表 = **未指定** = 沿用默认（注入全部）—— 缺省不收窄，是为了不改变既有行为：
    记忆注入本来就是一直生效的，前端没传就把它关掉属于静默的功能回退。

    ``all`` 折叠成空列表：服务端只需要「全量」与「按分类」两种状态，多一个
    显式全量值只会让下游到处判断它。
    """
    slots = _as_str_list((workbench_context or {}).get("memory_slots"))
    if "all" in slots:
        return []
    return slots
