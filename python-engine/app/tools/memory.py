"""Memory tools (remember / recall / forget / memory_search) 注册到本地工具注册表。

行为（记忆四层架构）：
- 使用 MemoryService（L2 长期记忆条目 + L3 摘要召回）
- 支持四类槽位：identity/preference/decision/fact
- 支持冲突检测与处理
- memory_search 走 L3 语义检索

**读写口径与记忆页一致**：``remember`` 写 ``user_memory_entries``（记忆页管理的同一张表），
``forget`` 也从那张表删。此前工具写的是 ``user_memory_profile``（个性化档案卡），
于是 Agent 明确「记住」的东西永远不出现在记忆页，用户也无从核对或删除。

用户身份取自工具执行上下文（app.tools.context，AgentRuntime.run 内设置）。
"""

from __future__ import annotations

import logging
from typing import Any

from app.memory.layers import SlotType
from app.tools.context import get_tenant_id, get_user_id
from app.tools.registry import registry

logger = logging.getLogger(__name__)


def _get_memory_service() -> Any:
    """获取 MemoryService 实例（从应用上下文中）。"""
    try:
        from app.memory.service import get_memory_service

        return get_memory_service()
    except ImportError:
        return None


def _resolve_slot(slot: str | None) -> SlotType:
    """把工具入参转成槽位枚举，非法值退回 FACT（工具层不抛错，避免打断 Agent）。"""
    if not slot:
        return SlotType.FACT
    try:
        return SlotType(str(slot).lower())
    except ValueError:
        return SlotType.FACT


async def remember(key: str, value: str, slot: str = "fact") -> dict[str, Any]:
    """记忆工具：保存用户长期记忆条目。

    Args:
        key: 记忆键。
        value: 记忆值。
        slot: 槽位类型。

    Returns:
        操作结果（``output`` 供 Agent 读取；``slot`` 便于调用方核对落库分类）。
    """
    if not key or not value:
        return {"error": "key and value are required"}

    svc = _get_memory_service()
    if svc is None:
        return {
            "output": f"Memory service not available. Cannot remember: {key}",
            "warning": "Memory service is not initialized. Please configure PostgreSQL.",
        }

    user_id = get_user_id()
    if not user_id:
        return {"error": "memory service requires user context (run via agent runtime)"}

    tenant_id = get_tenant_id() or "default"
    slot_type = _resolve_slot(slot)

    try:
        result = await svc.upsert(
            tenant_id=tenant_id,
            user_id=user_id,
            slot=slot_type.value,
            key=key,
            value=value,
            confidence=60,
            source="tool_written",
        )

        conflict = result.get("conflict")
        if conflict:
            # 冲突：已确认的值将被本次写入覆盖 —— 交给用户裁决，不静默覆盖。
            return {
                "output": (
                    f"⚠️ Conflict detected for [{slot_type.value}] {key}: "
                    f"Old value: {conflict.get('old_value')}, "
                    f"New value: {conflict.get('new_value')}. "
                    f"Please confirm with user before overwriting."
                ),
                "conflict": {
                    "conflict_id": conflict.get("conflict_id"),
                    "slot": conflict.get("slot"),
                    "key": conflict.get("key"),
                    "old_value": conflict.get("old_value"),
                    "new_value": conflict.get("new_value"),
                },
                "needs_confirmation": True,
                "slot": slot_type.value,
            }

        entry = result.get("entry") or {}
        out = f"✅ Remembered [{slot_type.value}] {key}: {value}"
        if not result.get("created", True):
            out += " (updated)"
        if result.get("duplicate_of"):
            out += f" (可能与「{result['duplicate_of'].get('key')}」重复)"
        return {"output": out, "success": True, "slot": slot_type.value, "entry": entry}

    except Exception as e:
        logger.error("Remember failed: %s", e)
        return {"error": f"Failed to remember: {str(e)}"}


async def recall(query: str = "", slot: str | None = None) -> dict[str, Any]:
    """回忆工具：召回用户记忆。

    Args:
        query: 查询文本（为空时只返回 L2 记忆清单）。
        slot: 可选，限制槽位。

    Returns:
        操作结果，包含 L2 记忆 + L3 摘要；``count`` 是当前用户的记忆条数。
    """
    svc = _get_memory_service()
    if svc is None:
        return {
            "output": "Memory service not available. Cannot recall memories.",
            "warning": "Memory service is not initialized.",
        }

    user_id = get_user_id()
    if not user_id:
        return {"error": "memory service requires user context (run via agent runtime)"}

    tenant_id = get_tenant_id() or "default"

    try:
        result = await svc.recall(tenant_id, user_id, query)

        parts = []

        # L2 长期记忆
        if result.profile_block:
            parts.append("📋 **用户记忆 (L2)**:")
            parts.append(result.profile_block)

        # L3 摘要
        if result.summary_items:
            parts.append("\n📚 **相关历史摘要 (L3)**:")
            for item in result.summary_items[:5]:
                parts.append(
                    f"- [{item.session_id}] {item.content[:100]}... (score: {item.score:.2f})"
                )

        # count 以「用户有多少条记忆」为准，而不是本次命中的摘要数 ——
        # 无 query 时后者恒为 0，会让调用方以为用户没有任何记忆。
        count = 0
        try:
            data = await svc.list_entries(tenant_id, user_id)
            count = int(data.get("total", 0))
        except Exception as e:
            logger.warning("Failed to count memory entries: %s", e)

        if not parts:
            return {"output": "No memories found yet.", "count": count}

        return {"output": "\n".join(parts), "count": count}

    except Exception as e:
        logger.error("Recall failed: %s", e)
        return {"error": f"Failed to recall: {str(e)}"}


async def forget(key: str, slot: str | None = None) -> dict[str, Any]:
    """遗忘工具：删除用户记忆条目（与记忆页同一张表）。

    Args:
        key: 记忆键。
        slot: 可选，限制槽位。

    Returns:
        操作结果。
    """
    if not key:
        return {"error": "key is required"}

    svc = _get_memory_service()
    if svc is None:
        return {
            "output": "Memory service not available. Cannot forget.",
            "warning": "Memory service is not initialized.",
        }

    user_id = get_user_id()
    if not user_id:
        return {"error": "memory service requires user context (run via agent runtime)"}

    tenant_id = get_tenant_id() or "default"

    try:
        deleted = await svc.forget_by_key(
            tenant_id,
            user_id,
            key,
            slot=slot,
        )

        if deleted:
            return {"output": f"✅ Forgot: {key}", "success": True, "deleted": deleted}
        return {"output": f"No memory found for key: {key}", "success": False, "deleted": 0}

    except Exception as e:
        logger.error("Forget failed: %s", e)
        return {"error": f"Failed to forget: {str(e)}"}


# memory_search token 预算硬上限（字符数）
_MEMORY_SEARCH_MAX_CHARS = 6000
# 单次 query 最大字符数，防止超长 query 导致嵌入失败
_MEMORY_SEARCH_MAX_QUERY = 500


async def memory_search(query: str, limit: int = 10) -> dict[str, Any]:
    """记忆搜索工具：语义搜索记忆（L3 摘要）。

    Args:
        query: 查询文本。
        limit: 返回数量限制（1–20，默认 10）。

    Returns:
        操作结果，包含 output / results / count。
    """
    # 输入校验
    if not query:
        return {"error": "query is required", "results": []}

    if len(query) > _MEMORY_SEARCH_MAX_QUERY:
        return {
            "error": f"query exceeds {_MEMORY_SEARCH_MAX_QUERY} chars limit",
            "results": [],
        }

    # 规范化 limit 范围
    try:
        limit = int(limit)
    except (TypeError, ValueError):
        limit = 10
    if limit < 1:
        limit = 1
    if limit > 20:
        limit = 20

    svc = _get_memory_service()
    if svc is None:
        return {"output": "Memory service not available.", "results": []}

    user_id = get_user_id()
    if not user_id:
        return {"error": "memory service requires user context", "results": []}

    tenant_id = get_tenant_id() or "default"

    try:
        result = await svc.recall(tenant_id, user_id, query, top_k=limit)

        # 从 summary_items 中提取 L3 结果
        l3_results = (
            result.summary_items[:limit] if hasattr(result, "summary_items") else []
        )

        if not l3_results:
            return {
                "output": f"No semantic memories found for: {query}",
                "results": [],
                "count": 0,
            }

        # 格式化结果，严格执行 token 字符预算
        formatted: list[dict[str, Any]] = []
        total_chars = 0
        for item in l3_results:
            content = item.content or ""
            # 单条截断，避免超长单条（含省略号总长度 ≤ 600）
            if len(content) > 597:
                content = content[:597] + "…"
            entry = {
                "id": item.id,
                "content": content,
                "topics": item.topics,
                "score": item.score,
                "session_id": item.session_id,
                "created_at": item.created_at,
            }
            entry_chars = len(content)
            # 保留至少 1 条结果，后续条目若超出预算则停止
            if formatted and total_chars + entry_chars > _MEMORY_SEARCH_MAX_CHARS:
                break
            formatted.append(entry)
            total_chars += entry_chars

        return {
            "output": f"Found {len(formatted)} memories for: {query}",
            "results": formatted,
            "count": len(formatted),
            "truncated": len(formatted) < len(l3_results),
        }

    except Exception as e:
        logger.error("Memory search failed: %s", e)
        return {"error": f"Search failed: {str(e)}", "results": []}


# ── 注册工具 ──────────────────────────────────────────────────────────

registry.register(
    name="remember",
    description=(
        "Save an important fact, decision, or preference to the user's persistent "
        "long-term memory (cross-session). slot: identity/preference/decision/fact."
    ),
    parameters={
        "type": "object",
        "properties": {
            "key": {
                "type": "string",
                "description": "Unique key for this memory (e.g. 'timezone')",
            },
            "value": {"type": "string", "description": "Memory content to remember"},
            "slot": {
                "type": "string",
                "enum": ["identity", "preference", "decision", "fact"],
                "description": "Memory category slot (default: fact)",
            },
        },
        "required": ["key", "value"],
    },
    handler=remember,
)

registry.register(
    name="recall",
    description=(
        "Retrieve the user's long-term memories. "
        "Returns both L2 memory entries and L3 semantic summaries."
    ),
    parameters={
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": "Search query to find matching memories",
                "default": "",
            },
            "slot": {
                "type": "string",
                "enum": ["identity", "preference", "decision", "fact"],
                "description": "Optional: restrict search to one slot",
            },
        },
    },
    handler=recall,
)

registry.register(
    name="forget",
    description="Remove a saved memory by key (optionally scoped to a slot).",
    parameters={
        "type": "object",
        "properties": {
            "key": {"type": "string", "description": "Key of the memory to forget"},
            "slot": {
                "type": "string",
                "enum": ["identity", "preference", "decision", "fact"],
                "description": "Optional: only forget within this slot",
            },
        },
        "required": ["key"],
    },
    handler=forget,
)

registry.register(
    name="memory_search",
    description=(
        "Perform semantic search across user's long-term memory. "
        "Returns ranked results with relevance scores."
    ),
    parameters={
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": "Natural language query to search for",
            },
            "limit": {
                "type": "integer",
                "description": "Maximum number of results to return (default: 10)",
                "default": 10,
            },
        },
        "required": ["query"],
    },
    handler=memory_search,
)
