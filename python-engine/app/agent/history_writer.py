"""会话历史的 Durable 写读 —— PostgreSQL `messages` 表。

**职责**：把引擎侧完整的中立消息序列（含 `tool_calls` 与 `tool` 角色）写入 PG，
并在读取时按 `seq` 还原。它是"压缩后可召回原文"的地基：

- Redis `session_cache` 有 2 小时 TTL，且 `session_store.append` 是**覆盖写** ——
  `runtime` 把压缩后的列表写回去，等于**唯一的完整副本被压缩版覆盖**。
- 本模块写入的是**只追加**的行：压缩不删行，只记 `session_folds`（后续步骤）。

四条硬约束
----------
1. **写前校验归属**：`SELECT 1 FROM sessions WHERE id=$1 AND tenant_id=$2`。
   引擎直连 PG 绕过了 Go 的 `ensureSessionOwned`，这条校验是唯一的租户边界。
2. **只追加**：只 `INSERT`。任何 `UPDATE` / `DELETE messages` 都是缺陷。
3. **失败不阻断**：写库失败只告警，本轮对话继续 —— 与 `result_store.store` 同一取向。
   宁可少一行历史，也不能让一次 DB 抖动把对话打挂。
4. **幂等**：用 `(session_id, seq)` 之外的稳定键去重不现实（seq 由序列分配），
   因此同一条消息重复提交会产生重复行 —— 调用方必须只在**每轮结束**调用一次。

为什么按 `seq` 而不是 `created_at` 读
--------------------------------------
`created_at` 是 `timestamp`（无时区），多副本同毫秒写入会重复。`seq` 是
`GENERATED ALWAYS AS IDENTITY`，单调且无并发争歧义。
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)

#: 单条消息 content 入库上限。与 tool_calls 的 256KB 上限同一取向：
#: 无上限会让一两轮对话就把表撑大，拖慢备份与 VACUUM。
MESSAGE_CONTENT_MAX_CHARS = 256_000

#: 一次读取返回的最大消息数。历史是用来重建上下文的，不是导出工具。
DEFAULT_HISTORY_LIMIT = 500


def _truncate(text: str, limit: int = MESSAGE_CONTENT_MAX_CHARS) -> str:
    if len(text) <= limit:
        return text
    return text[:limit] + f"\n[... truncated at {limit} chars, original {len(text)}]"


def _coerce_tenant(tenant_id: Any) -> int:
    """把租户 ID 收敛成 int；认不出的返回 0（= 不可用，调用方据此跳过写入）。

    `AgentTask.tenant_id` 标注为 `int`，但构造路径（`runtime_types.py:85` 的
    `data.get("tenant_id", "")`）与测试都能塞进任意值。直接 `int()` 会抛
    `ValueError` —— 而这个函数在对话收尾路径上，一次抛错就会把整轮 run 打成失败。
    """
    if isinstance(tenant_id, bool):
        return 0
    if isinstance(tenant_id, int):
        return tenant_id if tenant_id > 0 else 0
    try:
        value = int(str(tenant_id).strip())
    except (TypeError, ValueError):
        return 0
    return value if value > 0 else 0


async def _session_belongs_to_tenant(session_id: str, tenant_id: int) -> bool:
    """会话归属校验 —— **fail-closed**。

    查不到会话 / 查询异常 / 租户不匹配，一律返回 False。宁可拒绝写入，
    也不能把 A 租户的对话写进 B 租户的行里。
    """
    from app.db import get_pool

    try:
        pool = get_pool()
        if pool is None:
            return False
        row = await pool.fetchrow(
            "SELECT 1 AS ok FROM sessions WHERE id = $1 AND tenant_id = $2",
            session_id,
            tenant_id,
        )
        return row is not None
    except Exception as exc:  # noqa: BLE001 — 校验失败按不通过处理
        logger.warning("history_writer: session ownership check failed (%s): %s", session_id, exc)
        return False


async def _drop_existing_tool_calls(pool: Any, session_id: str, payload: list[tuple]) -> list[tuple]:
    """剔除本会话里已经存在的 tool 消息（按 tool_call_id）。

    overflow 重试与收尾路径都可能重复调用 `persist_messages`，而 `messages` 是
    只追加的（没有唯一约束兜底）。不去重的话，同一个工具结果会在上下文里出现两次，
    模型会以为自己执行了两遍。
    """
    ids = [row[3] for row in payload if row[3]]
    if not ids:
        return payload
    try:
        rows = await pool.fetch(
            "SELECT tool_call_id FROM messages WHERE session_id = $1 AND tool_call_id = ANY($2::text[])",
            session_id,
            ids,
        )
        seen = {r["tool_call_id"] for r in rows}
    except Exception as exc:  # noqa: BLE001 — 去重失败时宁可多写（历史完整优先）
        logger.warning("history_writer: dedupe lookup failed (%s): %s", session_id, exc)
        return payload
    if not seen:
        return payload
    return [row for row in payload if not row[3] or row[3] not in seen]


async def persist_messages(
    session_id: str,
    tenant_id: Any,
    messages: list[dict[str, Any]],
    *,
    turn_id: str = "",
    roles: set[str] | None = None,
) -> int:
    """把消息追加进 `messages`，返回写入行数。

    `roles` 限定只写哪些角色（None = 全部）。**runtime 调用时传 `{"tool"}`**：
    user / assistant 已由 Go 的 `SaveUserMessage` / `UpsertAssistantMessage` 写入，
    引擎再写一遍会产生重复行。而 Go 那条路径带有"提交时立即落库"的保证
    （SSE 中断也不丢用户消息），不能被替换。

    只写**本轮增量**由调用方决定：本函数不做去重，重复调用会重复插入。
    `system` 消息不入库 —— 它是每轮重建的（技能目录 / 记忆 / harness suffix 都会变），
    存下来会让"历史"里混进一堆互相矛盾的 system 段。

    失败返回 0 且只告警：历史缺失比对话中断轻得多。
    """
    # 入口处收敛一次：`tenant_id` 在构造路径上可能是任意值，而这里位于对话收尾，
    # 一次 `ValueError` 就会把整轮 run 打成失败。
    tenant_id = _coerce_tenant(tenant_id)
    if not session_id or tenant_id <= 0 or not messages:
        return 0

    from app.db import get_pool

    payload = [
        (
            m.get("role") or "user",
            _truncate(str(m.get("content") or "")),
            _tool_calls_json(m),
            str(m.get("tool_call_id") or "") or None,
            turn_id or None,
        )
        for m in messages
        if (m.get("role") or "") != "system"
        and (roles is None or (m.get("role") or "") in roles)
    ]
    if not payload:
        return 0

    # 隐私模式：no_retention 一律不落库。这是合规要求，不是性能优化 ——
    # `session_store` 在同一判定下跳过 Redis，这里必须同样跳过 PG。
    from app.middleware.privacy_middleware import is_no_retention

    if is_no_retention():
        logger.debug("history_writer: skip persist under no_retention (session=%s)", session_id)
        return 0

    try:
        if not await _session_belongs_to_tenant(session_id, tenant_id):
            logger.warning(
                "history_writer: refuse to persist, session not owned (session=%s tenant=%s)",
                session_id,
                tenant_id,
            )
            return 0

        pool = get_pool()
        if pool is None:
            return 0

        # 幂等：tool 角色按 tool_call_id 去重。轮次在 overflow / 重试下可能重复走
        # 收尾路径，重复插入会让同一个工具结果在上下文里出现两次。
        if roles is not None and roles == {"tool"}:
            payload = await _drop_existing_tool_calls(pool, session_id, payload)

        inserted = 0
        async with pool.acquire() as conn:
            async with conn.transaction():
                for role, content, tool_calls, tool_call_id, tid in payload:
                    await conn.execute(
                        "INSERT INTO messages "
                        "(id, session_id, role, content, tool_calls, tool_call_id, turn_id, created_at) "
                        "VALUES (gen_random_uuid()::text, $1, $2, $3, $4::jsonb, $5, $6, NOW())",
                        session_id,
                        role,
                        content,
                        tool_calls,
                        tool_call_id,
                        tid,
                    )
                    inserted += 1
        return inserted
    except Exception as exc:  # noqa: BLE001 — 写历史失败不阻断对话
        logger.warning("history_writer: persist failed (session=%s): %s", session_id, exc)
        return 0


async def load_message_content(
    session_id: str,
    tenant_id: Any,
    message_id: str,
    *,
    offset: int = 0,
    limit: int = 8000,
) -> tuple[str, int] | None:
    """按 `messages.id` 取回单条消息原文（分页），返回 `(片段, 原文总长)`。

    这是"压缩后原文可召回"的**长期**通道：`result_store` 走 Redis，TTL 只有 2 小时；
    而 `messages` 是持久表，过了一周仍能取回。

    **归属校验必须带 `session_id`**：只按 `id` 查的话，知道一个 message id
    就能读到别的会话里别人的原文 —— id 是可枚举的 UUID，不是能力凭证。
    """
    tenant_id = _coerce_tenant(tenant_id)
    if not session_id or not message_id or tenant_id <= 0:
        return None

    from app.db import get_pool

    try:
        if not await _session_belongs_to_tenant(session_id, tenant_id):
            logger.warning(
                "history_writer: refuse to recall, session not owned (session=%s tenant=%s)",
                session_id,
                tenant_id,
            )
            return None

        pool = get_pool()
        if pool is None:
            return None

        row = await pool.fetchrow(
            "SELECT content FROM messages WHERE id = $1 AND session_id = $2",
            message_id,
            session_id,
        )
    except Exception as exc:  # noqa: BLE001 — 读失败按"取不回"处理
        logger.warning("history_writer: recall failed (id=%s): %s", message_id, exc)
        return None

    if row is None:
        return None
    content = row["content"] or ""
    total = len(content)
    if limit and limit > 0:
        return content[offset : offset + limit], total
    return content[offset:], total


async def load_messages(
    session_id: str, tenant_id: Any, *, limit: int = DEFAULT_HISTORY_LIMIT
) -> list[dict[str, Any]]:
    """按 `seq` 升序取最近 `limit` 条消息，返回中立格式。

    取"最近 N 条"而不是"从头取"：长会话的早期内容早已被折叠，重建上下文
    需要的是尾部。返回前按 `seq` 升序，调用方拿到的顺序即时间顺序。

    归属校验同样 fail-closed：不属于该租户就返回空列表（而不是报错），
    让上层"看起来没历史"，而不是把一个 403 混进对话链路。
    """
    tenant_id = _coerce_tenant(tenant_id)
    if not session_id or tenant_id <= 0:
        return []

    from app.db import get_pool

    try:
        if not await _session_belongs_to_tenant(session_id, tenant_id):
            logger.warning(
                "history_writer: refuse to load, session not owned (session=%s tenant=%s)",
                session_id,
                tenant_id,
            )
            return []

        pool = get_pool()
        if pool is None:
            return []

        rows = await pool.fetch(
            "SELECT role, content, COALESCE(tool_calls::text, ''), "
            "COALESCE(turn_id::text, ''), COALESCE(tool_call_id, '') "
            "FROM messages WHERE session_id = $1 "
            "ORDER BY seq DESC LIMIT $2",
            session_id,
            limit,
        )
    except Exception as exc:  # noqa: BLE001 — 读失败按"无历史"处理
        logger.warning("history_writer: load failed (session=%s): %s", session_id, exc)
        return []

    # DESC 取尾 → 反转成时间顺序
    out: list[dict[str, Any]] = []
    for row in reversed(rows):
        msg: dict[str, Any] = {"role": row["role"], "content": row["content"]}
        raw_calls = row[2]
        if raw_calls:
            import json

            try:
                calls = json.loads(raw_calls)
            except (TypeError, ValueError):
                calls = None
            if isinstance(calls, list) and calls:
                normalized = _normalize_stored_tool_calls(calls)
                if normalized:
                    msg["tool_calls"] = normalized
        if row[3]:
            msg["turn_id"] = row[3]
        # tool 消息必须带 tool_call_id 才能与 assistant.tool_calls 配对；
        # 缺了它，`_ensure_valid_tool_sequence` 会把这条当孤儿删掉 ——
        # 于是"入库存了、读回来没了"。
        if row[4]:
            msg["tool_call_id"] = row[4]
        out.append(msg)
    if not out:
        return []

    await _backfill_tool_call_names(pool, session_id, out)
    return out


def _normalize_stored_tool_calls(calls: list[Any]) -> list[dict[str, Any]]:
    """把库里存的 tool_calls 归一成引擎期望的对象数组。

    **这是必需的，不是美化**：Go 写进来的 `messages.tool_calls` 是
    `[]string`（`json.Marshal(turnToolCallIDs)`，见 `submit_handler.go:757`），
    形如 `["call_1"]`；而引擎侧全部按 `tc.get("id")` 取值
    （`message_codec.make_message`、`compaction._ensure_valid_tool_sequence`）。
    对字符串调 `.get()` 会抛 `AttributeError` —— 一次历史读取就能打挂整轮对话。

    已经归一过的对象数组原样保留（幂等）。
    """
    out: list[dict[str, Any]] = []
    for item in calls:
        if isinstance(item, str):
            if item:
                # 名字暂时留空：真正的 tool_name / 参数在 tool_calls 表，
                # 由 `_backfill_tool_call_names` 回填。
                out.append({"id": item, "name": "", "arguments": ""})
            continue
        if isinstance(item, dict):
            if not item.get("id"):
                continue
            fn = item.get("function")
            if isinstance(fn, dict):
                out.append(
                    {
                        "id": str(item["id"]),
                        "name": str(fn.get("name") or ""),
                        "arguments": str(fn.get("arguments") or ""),
                    }
                )
            else:
                out.append(
                    {
                        "id": str(item["id"]),
                        "name": str(item.get("name") or ""),
                        "arguments": str(item.get("arguments") or ""),
                    }
                )
    return out


async def _backfill_tool_call_names(
    pool: Any, session_id: str, messages: list[dict[str, Any]]
) -> None:
    """从 `tool_calls` 表回填工具名与参数（原地修改）。

    库里的 `messages.tool_calls` 只存 id 集合（`submit_handler.go:588` 的注释），
    没有名字的话模型只会看到 `{"id": "call_1", "name": ""}` —— 它无法判断
    这是 read_file 还是 shell_exec，历史上下文基本不可用。

    查不到就保持空名字：**不臆造**。漏一次名字比猜错一个工具轻得多。
    """
    ids = [
        str(tc.get("id"))
        for m in messages
        for tc in (m.get("tool_calls") or [])
        if tc.get("id")
    ]
    if not ids:
        return
    try:
        rows = await pool.fetch(
            "SELECT id, tool_name, COALESCE(input::text, '') "
            "FROM tool_calls WHERE session_id = $1 AND id = ANY($2::text[])",
            session_id,
            ids,
        )
        meta = {r["id"]: (r["tool_name"], r["input"]) for r in rows}
    except Exception as exc:  # noqa: BLE001 — 回填失败不阻断，名字留空
        logger.warning("history_writer: tool name backfill failed (%s): %s", session_id, exc)
        return
    if not meta:
        return
    for m in messages:
        for tc in m.get("tool_calls") or []:
            known = meta.get(str(tc.get("id")))
            if not known:
                continue
            if not tc.get("name"):
                tc["name"] = known[0] or ""
            if not tc.get("arguments"):
                tc["arguments"] = known[1] or ""


def _tool_calls_json(message: dict[str, Any]) -> str | None:
    """把中立格式的 tool_calls 序列化成 JSON 字符串（jsonb 列）。"""
    calls = message.get("tool_calls")
    if not calls:
        return None
    import json

    try:
        return json.dumps(calls, ensure_ascii=False)
    except (TypeError, ValueError):
        return None
