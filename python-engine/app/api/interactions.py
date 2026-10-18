"""M7 人工介入闭环的读写端点（1B 的服务端半边）。

M7a/M7b 已经把"这一步在等人"落进了 `pending_interactions`，并以 `awaiting_input` 收尾；
这一对端点把它变成用户**看得见、答得上**的东西：

* `GET  /v1/agent/sessions/{session_id}/interactions` —— 该会话还等着人的介入（待办列表）；
* `POST /v1/agent/interactions/{interaction_id}/resolve` —— 用户给出答复（`pending → answered`）。

答复在这里**不执行任何东西**：`answered` 只是一张票，由续跑的那一轮经 `consume_grant`
取走（三条语义见 `app/agent/interactions.py`）。所以本端点写完就返回，"什么时候接着跑"
归提交入口 —— 前端答完发一轮 submit，引擎在新 run 开头接手挂起现场。

身份来源只有网关：`?user_id=&tenant_id=` 由 Go 侧从已验证 JWT claims 拼出来（客户端伪造的
同名头会被网关剥离，而 `app/middleware/auth.py` 又要求 query 身份必须带 `X-Internal-Token`）。
因此这里不再重复认证，只做"身份缺失就不查/不写"的兜底。

错误一律 200 + `{ok: false, error_code}`（与本仓其它引擎端点一致）：`not_found` /
`stale` / `invalid_decision` 都是**预期**结果，不是服务故障；网关据此映射成 404/409/400。
"""

from __future__ import annotations

import json
import logging
from typing import Any

from fastapi import APIRouter, Body, Query

logger = logging.getLogger(__name__)

router = APIRouter(tags=["agent-interactions"])

#: 单次列表上限：待办本来就少（一个会话最多几条），但入参来自外部，必须有界。
MAX_LIST_LIMIT = 50


def _identity(
    query_tenant: str, query_user: str, body: dict[str, Any] | None = None
) -> tuple[str, str]:
    """取身份：query 优先（网关代理形态），其次 body（内部直连形态）。

    两者都不猜：拿不到 `user_id` 时返回空串，调用方据此拒绝 —— 介入票是**授权**，
    "身份不明"绝不能变成"看全部"或"替别人答"。
    """
    src = body if isinstance(body, dict) else {}
    tenant = str(query_tenant or src.get("tenant_id") or "")
    user = str(query_user or src.get("user_id") or "")
    return tenant, user


@router.get("/v1/agent/sessions/{session_id}/interactions")
async def list_session_interactions(
    session_id: str,
    tenant_id: str = Query("", alias="tenant_id"),
    user_id: str = Query("", alias="user_id"),
    limit: int = Query(20, ge=1, le=MAX_LIST_LIMIT),
) -> dict[str, Any]:
    """列出该会话仍"等着人"的介入（`pending` / `answered`）。"""
    from app.agent import interactions

    if not user_id:
        return {"ok": False, "error_code": "unauthorized", "error": "user_id is required"}
    rows = await interactions.list_open(
        tenant_id=tenant_id, user_id=user_id, session_id=session_id, limit=limit
    )
    items = [
        {
            "id": row.get("id"),
            "kind": row.get("kind"),
            "status": row.get("status"),
            "tool_name": row.get("tool_name"),
            "tool_call_id": row.get("tool_call_id"),
            "request": row.get("request") or {},
            "decision": row.get("decision") or {},
            "reason": row.get("reason") or "",
            "created_at": row.get("created_at"),
            "expires_at": row.get("expires_at"),
        }
        for row in rows
    ]
    return {"ok": True, "session_id": session_id, "count": len(items), "items": items}


@router.post("/v1/agent/interactions/{interaction_id}/resolve")
async def resolve_interaction(
    interaction_id: str,
    body: dict[str, Any] = Body(default_factory=dict),
    tenant_id: str = Query("", alias="tenant_id"),
    user_id: str = Query("", alias="user_id"),
) -> dict[str, Any]:
    """将用户的答复落到那张票上（`pending → answered`）。

    `response` 的形状由那一行的 `kind` 决定，不是由调用方声称：

    * `approval`：沿用审批三态（`decision=approve|reject|edit`，兼容旧的 `approved: bool`），
      解析**委托** `runtime.parse_approval_payload` —— 与实时审批端点同源，避免两份实现漂移；
    * `question`：`{"answer": "..."}` 或 `{"declined": true, "reason": "..."}`。

    答错形状一律拒绝（`invalid_decision`）而不是"先存着，续跑时再说"：一张存进去就取不出
    来的票会白占住这个会话的唯一名额（`ux_pending_interactions_open`），用户看到的是
    "答了却没反应"，而真正的原因在几小时后的另一条日志里。
    """
    from app.agent import interactions
    from app.agent.runtime import parse_approval_payload

    tenant, user = _identity(tenant_id, user_id, body)
    if not user:
        return {"ok": False, "error_code": "unauthorized", "error": "user_id is required"}
    session_id = str(body.get("session_id") or "")
    if not session_id:
        return {
            "ok": False,
            "error_code": "invalid_request",
            "error": "session_id is required (it identifies which run to resume)",
        }
    response = body.get("response")
    if not isinstance(response, dict) or not response:
        return {"ok": False, "error_code": "invalid_request", "error": "response is required"}

    row = await interactions.peek(
        interaction_id=interaction_id, tenant_id=tenant, user_id=user
    )
    if row is None:
        # 不存在 / 不是这个人的：不区分（区分本身会泄漏"别人的介入存在与否"）
        return {"ok": False, "error_code": "not_found", "error": "interaction not found"}
    if str(row.get("session_id") or "") != session_id:
        # 票与声称的会话必须同源，否则网关会唤醒一个**没有挂起现场**的会话：答复落进了
        # 一张别的会话的票里，用户等的那一轮却永远不会继续。
        return {"ok": False, "error_code": "invalid_request", "error": "session_id mismatch"}
    status = str(row.get("status") or "")
    if status != interactions.STATUS_PENDING:
        return {
            "ok": False,
            "error_code": "stale",
            "error": f"interaction is no longer pending (status={status})",
            "status": status,
        }

    kind = str(row.get("kind") or "")
    if kind == interactions.KIND_APPROVAL:
        decision = parse_approval_payload(
            approved=response.get("approved"),
            decision=response.get("decision"),
            arguments=response.get("arguments"),
            reason=response.get("reason"),
        )
        if isinstance(decision, str):
            return {"ok": False, "error_code": "invalid_decision", "error": decision}
        payload: dict[str, Any] = json.loads(decision.to_json())
    elif kind == interactions.KIND_QUESTION:
        answer = response.get("answer")
        if isinstance(answer, str) and answer.strip():
            payload = {"answer": answer}
            reason = str(response.get("reason") or "")
            if reason:
                payload["reason"] = reason
        elif response.get("declined") is True:
            payload = {"declined": True, "reason": str(response.get("reason") or "")}
        else:
            return {
                "ok": False,
                "error_code": "invalid_decision",
                "error": "answer is required (or set declined=true)",
            }
    else:
        # 库里的 kind 认不出来 = 写入方与本读取方对同一张表的理解已经漂移。
        # 这时**任何**答复都可能是错的，如实报故障比猜一个形状更负责。
        logger.error("pending interaction has unknown kind=%r (id=%s)", kind, interaction_id)
        return {"ok": False, "error_code": "storage_error", "error": "unknown interaction kind"}

    resolved = await interactions.resolve(
        interaction_id=interaction_id,
        tenant_id=tenant,
        user_id=user,
        decision=payload,
        reason=str(payload.get("reason") or response.get("reason") or ""),
    )
    if resolved is None:
        # peek 与 resolve 之间那一行被关掉了（实时通道刚收到答复 / 过期巡检扫到），
        # 或者它的 24h 窗口已过 —— `resolve` 的谓词里就有 `expires_at > now()`。
        # 不重试：票只剩一次，重复答复的两种结局都该是同一个可见答案。
        return {
            "ok": False,
            "error_code": "stale",
            "error": "interaction is no longer answerable (answered, consumed or expired)",
        }
    return {
        "ok": True,
        "data": {
            "id": resolved.get("id"),
            "kind": resolved.get("kind"),
            "session_id": resolved.get("session_id"),
            "tool_name": resolved.get("tool_name"),
            "status": interactions.STATUS_ANSWERED,
            "decision": resolved.get("decision") or {},
        },
    }
