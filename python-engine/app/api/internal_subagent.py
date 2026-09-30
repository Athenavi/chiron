"""S3：远端子 agent 的**接收端**（内网端点，方案 01 §4.3）。

认证形态是 **body-身份端点**：走 `X-Internal-Token`（与网关代理路径同一凭据，由
`app/middleware/auth.py` 校验），身份由本端点自己从 body 取 —— 与 `/v1/agent/submit` 同形态，
因此不需要额外的头剥离逻辑（该形态的存在前提正是"网关已剥离客户端可伪造的同名头"）。

**接收方重新校验**（方案 §4.3 的安全前置，逐条落地）：

* `tenant_id` / `user_id` **必须非空** —— 拒绝"无归属"的跨实例调用（否则无从做租户隔离与审计）；
* **不信任请求体里的任何"已授权"标记**：只读与否由本实例按 `allow_write` 重新决定，
  不接受调用方声称的"已批准"；
* 深度受 `MAX_DEPTH` 约束 —— 远端不得成为绕过递归限制的通道；
* 结果一律经 `to_tool_payload()` 返回，其中的 `output` 已带 `<subagent-result>` 不可信包裹
  （调用侧还会再补一道，见 `app/subagent/remote.py`）。
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter
from pydantic import BaseModel

logger = logging.getLogger(__name__)

router = APIRouter(tags=["internal"])


class InternalSubagentRequest(BaseModel):
    """跨实例委派请求（**身份必填**：这是接收方重新校验的依据）。"""

    task: str
    tenant_id: str = ""
    user_id: str = ""
    session_id: str = ""
    mode: str = "normal"
    max_turns: int = 5
    allow_write: bool = False
    #: A4（方案 04）：per-call **收窄**（`None` = 不收窄；空列表 = 不允许任何工具）。
    #: 注意方向：这是**收窄**而非授权，所以与本文档开头的"不信任请求体里的授权标记"不冲突
    #: —— 远端**只能要得更少**，天花板仍由本实例按 Profile 决定。
    call_tools: list[str] | None = None
    depth: int = 0


@router.post("/v1/internal/subagent/run")
async def internal_subagent_run(body: InternalSubagentRequest) -> dict[str, Any]:
    """在**本实例**执行一次远端委派（同步，返回工具载荷形态的结果）。"""
    from app.tools.subagent import MAX_DEPTH, MAX_TURNS_CAP

    if not (body.task or "").strip():
        return {"ok": False, "error": "task is required"}
    if not body.tenant_id or not body.user_id:
        # 身份重校验：无归属的跨实例调用一律拒绝（宁可让调用方回退本实例）
        logger.warning("internal subagent rejected: missing tenant/user identity")
        return {"ok": False, "error": "tenant_id and user_id are required"}

    depth = max(0, int(body.depth or 0))
    if depth >= MAX_DEPTH:
        return {"ok": False, "error": f"delegation depth exceeded (max {MAX_DEPTH})"}

    from app.main import get_gateway

    gateway = await get_gateway()
    if gateway is None:
        return {"ok": False, "error": "engine gateway unavailable"}

    from app.agent.subagent_runner import SubAgentRunner
    from app.subagent.store import SubagentRunStore

    pool = None
    store = None
    try:
        from app.db import get_pool

        pool = get_pool()
        if pool is not None:
            store = SubagentRunStore(pool)
    except Exception as exc:  # noqa: BLE001 — 落库能力缺失不该阻断委派（与工具层同语义）
        logger.warning("internal subagent: db unavailable (run will not be persisted): %s", exc)

    runner = SubAgentRunner(
        gateway,
        store=store,
        pool=pool,
        depth=depth,
        parent_session_id=body.session_id,
        tenant_id=body.tenant_id,
        user_id=body.user_id,
        background=False,
        allow_write=bool(body.allow_write),
        call_tools=body.call_tools,
    )
    try:
        result = await runner.run(
            body.task,
            mode=body.mode or "normal",
            max_turns=max(1, min(int(body.max_turns or 5), MAX_TURNS_CAP)),
        )
    except Exception as exc:  # noqa: BLE001 — 单次委派失败以结构化错误回给调用方
        logger.warning("internal subagent run failed (session=%s): %s", body.session_id, exc)
        return {"ok": False, "error": f"subagent run failed: {str(exc)[:200]}"}

    payload = result.to_tool_payload()
    return {"ok": True, "data": payload if isinstance(payload, dict) else {}}
