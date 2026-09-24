"""Webhook 事件总线 — Python 引擎事件 → Go 网关投递。

事件类别：
- agent.start: Agent 开始推理
- agent.complete: Agent 完成推理
- agent.error: Agent 推理出错
- knowledge.ingest: 知识库文件处理完成
- knowledge.ingest_error: 知识库文件处理失败
- system.alert: 系统告警（如 LLM 调用失败率过高）

用法：
  from app.event_bus import emit_event
  await emit_event("agent.complete", {"session_id": ..., "tokens_used": ...})
"""

from __future__ import annotations

import logging
import uuid
from typing import Any

from app.config import settings

logger = logging.getLogger(__name__)


async def emit_event(event_type: str, payload: dict[str, Any], tenant_id: str = "") -> bool:
    """向网关推送 Webhook 事件。

    Args:
        event_type: 事件类型，如 "agent.complete", "knowledge.ingest"
        payload: 事件负载 dict
        tenant_id: 租户 ID（可选；从 payload 中提取或使用默认值）

    Returns:
        bool: 是否成功推送
    """
    if not settings.internal_token:
        logger.debug("emit_event skipped: internal_token not configured")
        return False

    tenant = tenant_id or payload.get("tenant_id", "default")
    body = {
        "id": uuid.uuid4().hex[:16],  # 事件幂等键（网关透传 X-Webhook-Event-Id，接收方去重）
        "tenant_id": tenant,
        "type": event_type,
        "payload": payload,
    }

    import httpx

    url = f"{settings.gateway_internal_url.rstrip('/')}/v1/internal/webhook-event"
    headers = {
        "X-Internal-Token": settings.internal_token,
        "Content-Type": "application/json",
    }

    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            resp = await client.post(url, json=body, headers=headers)
            if resp.is_success:
                logger.debug("webhook event emitted: %s", event_type)
                return True
            logger.warning(
                "webhook event failed: type=%s status=%s body=%s",
                event_type, resp.status_code, resp.text[:200],
            )
            return False
    except Exception as e:
        logger.warning("webhook event transport error: type=%s error=%s", event_type, e)
        return False


# ── 便捷事件函数 ──


async def emit_agent_start(session_id: str, user_id: str, tenant_id: str, **extra: Any) -> bool:
    """Agent 开始推理事件"""
    payload = {
        "session_id": session_id,
        "user_id": user_id,
        "tenant_id": tenant_id,
        **extra,
    }
    return await emit_event("agent.start", payload, tenant_id=tenant_id)


async def emit_agent_complete(
    session_id: str, user_id: str, tenant_id: str,
    tokens_used: int = 0, duration_ms: int = 0, **extra: Any,
) -> bool:
    """Agent 完成推理事件"""
    payload = {
        "session_id": session_id,
        "user_id": user_id,
        "tenant_id": tenant_id,
        "tokens_used": tokens_used,
        "duration_ms": duration_ms,
        **extra,
    }
    return await emit_event("agent.complete", payload, tenant_id=tenant_id)


async def emit_agent_error(
    session_id: str, user_id: str, tenant_id: str,
    error: str, **extra: Any,
) -> bool:
    """Agent 推理出错事件"""
    payload = {
        "session_id": session_id,
        "user_id": user_id,
        "tenant_id": tenant_id,
        "error": error[:2000],
        **extra,
    }
    return await emit_event("agent.error", payload, tenant_id=tenant_id)


async def emit_knowledge_ingest(
    kb_id: str, doc_id: str, doc_name: str, tenant_id: str, **extra: Any,
) -> bool:
    """知识库文档处理完成事件"""
    payload = {
        "kb_id": kb_id,
        "doc_id": doc_id,
        "doc_name": doc_name,
        "tenant_id": tenant_id,
        **extra,
    }
    return await emit_event("knowledge.ingest", payload, tenant_id=tenant_id)


async def emit_knowledge_ingest_error(
    kb_id: str, doc_id: str, doc_name: str, tenant_id: str, error: str, **extra: Any,
) -> bool:
    """知识库文档处理失败事件"""
    payload = {
        "kb_id": kb_id,
        "doc_id": doc_id,
        "doc_name": doc_name,
        "tenant_id": tenant_id,
        "error": error[:2000],
        **extra,
    }
    return await emit_event("knowledge.ingest_error", payload, tenant_id=tenant_id)
