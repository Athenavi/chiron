"""M7 人工介入闭环：审批 / `ask_user` 的**落库现场**（1A）。

补的是 §5.1 里最后一条"正确性=可见性"的缺口：在 M6 之前，"这一步需要你确认"只活在
发起它的那个进程内存里（`AgentRuntime._pending_approvals` 的 Future + 一条 Redis 决策键）。
进程一死，用户既看不到"在等你"，也看不到"等的是哪一步"；M6 让无人值守回合不再空等，
但看到的仍然只是一句拒绝，不是一个可以回答的问题。

本模块是 `pending_interactions` 表（迁移 ``0018``）的唯一写入方，语义三条：

1. **状态机单向**：`pending → answered → consumed`，旁路只有 `expired`。
   `answered → consumed` 是**一次原子 UPDATE**（``consume_grant``）：批准只能被一个回合取走，
   两个回合并列续跑时第二个拿到 None ⇒ 它会重新问，而不是重复执行。
2. **认票认参数，不认 id**：续跑的那一轮是新 run，模型完全可能把同一个调用排成新的
   ``tool_call_id``。因此授权绑定 ``(session_id, kind, tool_name, args_hash)`` ——
   **参数变了就是另一件事**，必须重新确认（这是 C5 二次校验已有的原则，这里只是把它跨进程化）。
3. **一切失败都回落 M6**：写不进去 / 读不出来时，调用方退回"无人值守 ⇒ 即时拒绝"，
   **不会**出现"没人批准却执行了"。持久通道是增强路径，不是许可来源。

Redis 决策键（`approval:{tc_id}` / `answer:{tc_id}`）继续负责它擅长的事：**同一次运行内**
跨副本把决定送到正在空等的那个协程。两者不互相替换 —— 一个解决"进程还活着但请求落到了
另一个副本"，一个解决"进程没了 / 用户几小时后才看到"。
"""

from __future__ import annotations

import json
import logging
import uuid
from typing import Any

logger = logging.getLogger(__name__)

#: 人工介入类型。前两类形状相同（一个问题 + 一个答复），差别只在答复载荷，因此共用一张表。
KIND_APPROVAL = "approval"
KIND_QUESTION = "question"
#: M15c：一条任务线花到上限 ⇒ 线被暂停，并请人来决定"提到多少"。
#:
#: 它与前两类**不是一回事**，共用这张表只共用"看得见、答得上"这半条通道：
#:
#: * approval/question 的答复是一张**授权票**，由续跑的那一轮经 `consume_grant` 取走；
#: * budget 没有"续跑的那一轮"可以接手 —— 越界的那一段已经按失败收尾（子 Agent 跑在合成会话
#:   `sub:<父>:<run>` 里、没有 `agent_runs` 行，`_register_interaction` 因此不会为它挂起）。
#:   答复它等于**写一次控制态**（提上限 + 清暂停），写完这条线之后的每个新段就自己放行；
#:   所以它走 `settle`（一次性生效并关掉），故意不走 `resolve`：停在 `answered` 的票会一直占住
#:   `ux_pending_interactions_open` 的名额，下次再耗尽时新票就**创建不出来**了 —— 用户看到的是
#:   "线停了却没有待办"。
#: * 不答复 = 不提额 = 线继续停着，票保持 `pending`（24h 后过期；过期后再撞一次闸门会重新生成
#:   一张，因为 `line_paused` 也会带出票）。这里**没有 decline**：拒绝提额与"还没决定"在效果上
#:   完全相同（都该继续停），做一个会把票关掉却不改变任何事的按钮只会制造"我处理过了"的错觉。
KIND_BUDGET = "budget"
VALID_KINDS: frozenset[str] = frozenset({KIND_APPROVAL, KIND_QUESTION, KIND_BUDGET})

STATUS_PENDING = "pending"
STATUS_ANSWERED = "answered"
STATUS_CONSUMED = "consumed"
STATUS_EXPIRED = "expired"

#: 未完成（仍占用"同一会话同一调用只有一个待确认"这条唯一约束）的状态
OPEN_STATUSES: tuple[str, ...] = (STATUS_PENDING, STATUS_ANSWERED)

#: 现场有效期（秒）：与 `agent_runs` 的冷恢复窗口（``COLD_RESUME_WINDOW_SECONDS``）对齐。
#: 超出它，快照本身已判 `abandoned` —— 留着一条"可以批准"的票只会让用户批准一件
#: 早已无法续跑的事。到期不是删除（审计要留痕），而是转成 `expired` 这个**可见**的状态。
TTL_SECONDS = 24 * 3600


def _pool() -> Any:
    from app.db import get_pool

    return get_pool()


def _rowcount(result: Any) -> int:
    """从 asyncpg 的 `UPDATE n` / `INSERT 0 n` 状态串里取行数。"""
    parts = str(result or "").split()
    if len(parts) >= 2 and parts[0] in ("UPDATE", "DELETE"):
        try:
            return int(parts[1])
        except ValueError:
            return 0
    if len(parts) == 3 and parts[0] == "INSERT":
        try:
            return int(parts[2])
        except ValueError:
            return 0
    return 0


def _decode(raw: Any) -> dict[str, Any]:
    """jsonb 列可能是 str（文本协议）也可能已是 dict（二进制协议）—— 统一成 dict。"""
    if raw is None:
        return {}
    if isinstance(raw, (str, bytes, bytearray)):
        try:
            parsed = json.loads(bytes(raw).decode() if isinstance(raw, (bytes, bytearray)) else raw)
        except (json.JSONDecodeError, TypeError, UnicodeDecodeError):
            return {}
        return parsed if isinstance(parsed, dict) else {}
    if isinstance(raw, dict):
        return dict(raw)
    return {}


async def create(
    *,
    tenant_id: str,
    user_id: str,
    session_id: str,
    kind: str,
    tool_call_id: str,
    tool_name: str = "",
    args_hash: str = "",
    request: dict[str, Any] | None = None,
    run_token: str = "",
    turn_id: str = "",
    ttl_seconds: int = TTL_SECONDS,
) -> str:
    """登记一次"正在等人"，返回行 id；**写不进去返回空串**（调用方回落 M6）。

    `kind` 不在 `VALID_KINDS` 里时直接返回空串：这不是"参数校验的礼貌"，而是因为一个拼错的
    kind 会写进一条**永远匹配不上**的授权（`consume_grant` 按 kind 认票），等于静默丢失一次
    人工介入。

    **每一条返回空串的路径都要留下可见痕迹**（warn/error 日志 + `interaction_create_failed_total`）：
    approval/question 失败只是"回落 M6 的即时拒绝"，而 M15c 的 budget 票失败意味着"一条被暂停的线
    没有任何人能看见" —— 线停着、待办为空，运维只会看到子 Agent 反复失败。返回值仍然是空串
    （改成异常会把一条增强路径变成能打断对话的东西），但**不再静默**。

    `ON CONFLICT … DO UPDATE … WHERE status = 'pending'` 两个方向都要：

    * 同一个 ``tool_call_id`` 被**重新问一次**（模型换了说法、参数不变）：复用那一行并刷新
      展示内容，否则唯一索引会让第二次询问**静默失败**；
    * 但那一行若已是 `answered`（用户答了、还没被回合取走）：`WHERE status = 'pending'` 挡住
      覆盖 —— **已收到的答复不能被一次重新提问擦掉**。此时 `RETURNING` 无行 ⇒ 返回空串，
      调用方按"没有持久通道"处理（宁可拒绝对待答复的那一次执行，也不猜用户想要什么）。
    """
    from app.observability.metrics import INTERACTION_CREATE_FAILED

    def _fail(reason: str, msg: str, *args: Any) -> str:
        INTERACTION_CREATE_FAILED.labels(reason=reason).inc()
        logger.warning(msg, *args)
        return ""

    if kind not in VALID_KINDS:
        # 拼错的 kind 是**写侧的 bug**，不是运行时状况：按 error 记，别让它混在告警洪水里
        INTERACTION_CREATE_FAILED.labels(reason="unknown_kind").inc()
        logger.error("pending interaction rejected: unknown kind=%r", kind)
        return ""
    if not session_id or not tool_call_id:
        return _fail(
            "missing_keys",
            "pending interaction rejected: session_id/tool_call_id required (kind=%s)",
            kind,
        )
    try:
        pool = _pool()
        if pool is None:
            return _fail(
                "no_pool", "pending interaction create skipped: no PostgreSQL pool (kind=%s)", kind
            )
        row = await pool.fetchrow(
            """
            INSERT INTO pending_interactions
                (id, tenant_id, user_id, session_id, run_id, turn_id, kind,
                 tool_call_id, tool_name, args_hash, request, status,
                 created_at, updated_at, expires_at)
            VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11::jsonb,
                    'pending', now(), now(), now() + make_interval(secs => $12::double precision))
            ON CONFLICT (session_id, tool_call_id)
                WHERE status IN ('pending', 'answered')
            DO UPDATE
               SET request = EXCLUDED.request,
                   args_hash = EXCLUDED.args_hash,
                   tool_name = EXCLUDED.tool_name,
                   run_id = EXCLUDED.run_id,
                   turn_id = EXCLUDED.turn_id,
                   updated_at = now(),
                   expires_at = now() + make_interval(secs => $12::double precision)
             WHERE pending_interactions.status = 'pending'
            RETURNING id
            """,
            uuid.uuid4().hex,
            tenant_id or None,
            user_id or None,
            session_id,
            run_token or None,
            turn_id or None,
            kind,
            tool_call_id,
            tool_name or "",
            args_hash or "",
            json.dumps(request or {}, ensure_ascii=False),
            # `make_interval(secs => double precision)`：asyncpg 会按 float8 绑定该参数，
            # 传 int 会直接报类型错 —— 这里必须是 float。
            float(max(60, int(ttl_seconds or TTL_SECONDS))),
        )
        if row is None:
            # 唯一索引命中、而那一行已是 `answered`：票只有一张，不覆盖（见上面 docstring）。
            # 对 budget 这类"一次性"的票，这条路径意味着**已经有一张答过却没生效的票**占着名额 ——
            # 所以它必须可见，否则暂停的线从此没人能再提额。
            return _fail(
                "conflict_answered",
                "pending interaction not written: an answered ticket already holds this slot "
                "(session=%s kind=%s tool_call_id=%s)",
                session_id,
                kind,
                tool_call_id,
            )
        interaction_id = str(row["id"])
        # M21：票已经落库 ⇒ 再广播一条"多了一张待办"（失败不影响落票，见函数注释）。
        await _publish_pending_event(
            interaction_id=interaction_id,
            tenant_id=tenant_id or "",
            user_id=user_id or "",
            session_id=session_id,
            kind=kind,
            request=request,
        )
        return interaction_id
    except Exception as exc:  # noqa: BLE001 — 持久通道失败不改变对话行为（回落 M6 的即时拒绝）
        return _fail(
            "storage_error",
            "pending interaction create failed (session=%s): %s",
            session_id,
            exc,
        )


async def resolve(
    *,
    interaction_id: str,
    tenant_id: str,
    user_id: str,
    decision: dict[str, Any],
    reason: str = "",
) -> dict[str, Any] | None:
    """用户给出答复：`pending → answered`（**原子**，返回该行摘要）。

    返回 None 只有三种情况：不存在 / 不是这个人的 / 已经不 `pending`（已答、已消费、已过期）。
    调用方把它们一律当作 404/409 处理是**正确**的，不做区分 —— 区分本身会泄漏"别人的介入
    存在与否"。

    `tenant_id` 与 `user_id` 都进 WHERE：介入是**授权**（"这个参数组合可以执行"），
    跨用户命中等于把别人的批准拿来给自己的调用放行。
    """
    if not interaction_id:
        return None
    try:
        pool = _pool()
        if pool is None:
            return None
        row = await pool.fetchrow(
            """
            UPDATE pending_interactions
               SET status = 'answered',
                   decision = $4::jsonb,
                   reason = $5,
                   decided_by = $3,
                   updated_at = now()
             WHERE id = $1
               AND ($2 = '' OR tenant_id = $2)
               AND user_id = $3
               AND status = 'pending'
               AND expires_at > now()
            RETURNING id, kind, session_id, run_id, turn_id,
                      tool_call_id, tool_name, args_hash, request, decision
            """,
            interaction_id,
            tenant_id or "",
            user_id or "",
            json.dumps(decision or {}, ensure_ascii=False),
            (reason or "")[:2000],
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("pending interaction resolve failed (id=%s): %s", interaction_id, exc)
        return None
    if row is None:
        return None
    out = {str(k): row[k] for k in row.keys()}
    out["request"] = _decode(out.get("request"))
    out["decision"] = _decode(out.get("decision"))
    return out


async def peek(
    *, interaction_id: str, tenant_id: str, user_id: str
) -> dict[str, Any] | None:
    """按 id 读一张**自己的**票（`{id, kind, status, session_id, tool_name, args_hash, request}`）。

    存在它是为了答复端点能在写入前做两件**必须**的事：按行上的 `kind` 校验答复形状（否则
    存进去一张取不出来的票），以及核对"这张票确实属于用户声称的那个会话"。

    `user_id` 是硬条件（空值匹配不到任何行，因为建表时 `user_id` 来自已验证身份）：跨用户
    读别人的介入 = 读出"哪一步正在等人"，那本身就是待办信息。这里能区分 `not_found` 与
    `stale` 也只对**票的主人**成立，不构成信息泄漏。
    """
    if not interaction_id or not user_id:
        return None
    try:
        pool = _pool()
        if pool is None:
            return None
        row = await pool.fetchrow(
            """
            SELECT id, kind, status, session_id, tool_name, args_hash, request
              FROM pending_interactions
             WHERE id = $1
               AND ($2 = '' OR tenant_id = $2)
               AND user_id = $3
            """,
            interaction_id,
            tenant_id or "",
            user_id,
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("pending interaction peek failed (id=%s): %s", interaction_id, exc)
        return None
    if row is None:
        return None
    out = {str(k): row[k] for k in row.keys()}
    out["request"] = _decode(out.get("request"))
    return out


async def consume_grant(
    *,
    tenant_id: str,
    session_id: str,
    kind: str,
    tool_name: str,
    args_hash: str,
) -> dict[str, Any] | None:
    """续跑回合取走一次授权：`answered → consumed`（**单次消费**）。

    命中返回 `{decision, reason, decided_by, id}`；没有可用授权返回 None。

    并发下为什么不需要显式加锁：PG 在 READ COMMITTED 下对被更新的行做 EPQ ——
    第二个 `UPDATE … WHERE status = 'answered'` 会**等锁后重新判定谓词**，此时行已是
    `consumed`，于是 0 行命中。所以"批准生效两次"在 SQL 层面就写不出来。

    `args_hash` 为空时**直接不匹配**：空哈希意味着"没有参数指纹"，而"没有指纹的授权"
    可以匹配这个工具名下的任何一次调用 —— 那是绕过确认，不是人工介入。
    """
    if not session_id or not args_hash or kind not in VALID_KINDS:
        return None
    try:
        pool = _pool()
        if pool is None:
            return None
        row = await pool.fetchrow(
            """
            UPDATE pending_interactions
               SET status = 'consumed', updated_at = now()
             WHERE id = (
                       SELECT id FROM pending_interactions
                        WHERE session_id = $1
                          AND ($2 = '' OR tenant_id = $2)
                          AND kind = $3
                          AND tool_name = $4
                          AND args_hash = $5
                          AND status = 'answered'
                          AND expires_at > now()
                        ORDER BY created_at DESC
                        LIMIT 1
                   )
               AND status = 'answered'
            RETURNING id, decision, reason, decided_by
            """,
            session_id,
            tenant_id or "",
            kind,
            tool_name or "",
            args_hash,
        )
    except Exception as exc:  # noqa: BLE001 — 取不到授权 = 没批过，调用方按"未获批准"处理
        logger.warning("grant consume failed (session=%s): %s", session_id, exc)
        return None
    if row is None:
        return None
    out = {str(k): row[k] for k in row.keys()}
    out["decision"] = _decode(out.get("decision"))
    return out


async def settle(
    *,
    interaction_id: str,
    decision: dict[str, Any],
    reason: str = "",
    decided_by: str = "",
    tenant_id: str = "",
    user_id: str = "",
) -> bool:
    """**实时通道**给出了答复：把现场一并记上答复并直接关掉（`pending`/`answered` → `consumed`）。

    这条是必需的，不是"顺手写个审计"：如果实时路径（前端点确认 → Redis 决策键）答完之后
    那一行还留在 `pending`，就会有三个可见后果 —— 列表里挂着一个早已答完的问题、用户可能
    再答一次、而第二次答复会变成一张**将来**能被同类调用取走的授权票。所以"答复已经生效"
    必须同时把行关掉。

    `consume_grant` 与它的区别只在**谁**拿到了答复：`consume_grant` 是"续跑回合来取票"
    （`answered → consumed`，票只有一次），`settle` 是"发起它的那一轮自己收到了答复"
    （`pending → consumed`，票不进入下一个回合）。

    已是 `consumed` / `expired` 的行不会被改写（返回 False）：一张已经被取走的票不该因为
    一次迟到的实时答复而复活。

    ``tenant_id`` / ``user_id`` 是 M15c 加的**可选**归属谓词（空串 = 不按它过滤，与既有调用方
    逐字等价）：`resolve` 一直是按"这张票是不是这个人的"来改状态的，而 budget 的答复要先写
    `run_lineage_ledger` 再关票 —— 一条"按 id 就能关任何人的票"的通道会让那道写也变成可被
    他人触发的前置步骤。HTTP 侧因此两者都传。
    """
    if not interaction_id:
        return False
    try:
        pool = _pool()
        if pool is None:
            return False
        result = await pool.execute(
            """
            UPDATE pending_interactions
               SET status = 'consumed',
                   decision = $2::jsonb,
                   reason = $3,
                   decided_by = COALESCE(NULLIF($4, ''), decided_by),
                   updated_at = now()
             WHERE id = $1
               AND status IN ('pending', 'answered')
               AND ($5 = '' OR tenant_id = $5)
               AND ($6 = '' OR user_id = $6)
            """,
            interaction_id,
            json.dumps(decision or {}, ensure_ascii=False),
            (reason or "")[:2000],
            decided_by or "",
            tenant_id or "",
            user_id or "",
        )
    except Exception as exc:  # noqa: BLE001 — 收尾失败不影响这一轮已经拿到的答复
        logger.warning("pending interaction settle failed (id=%s): %s", interaction_id, exc)
        return False
    return _rowcount(result) > 0


async def list_open(
    *, tenant_id: str, user_id: str, session_id: str, limit: int = 20
) -> list[dict[str, Any]]:
    """列出该会话"还等着人"的介入（网关列表路由用）。

    只回 `pending` / `answered`：`consumed` / `expired` 已经与用户无关（前者已被回合取走，
    后者连快照都续不上了），留在列表里只会让用户去批准一件已经不存在的事。
    """
    if not session_id:
        return []
    try:
        pool = _pool()
        if pool is None:
            return []
        rows = await pool.fetch(
            """
            SELECT id, kind, status, run_id, turn_id, tool_call_id, tool_name,
                   args_hash, request, decision, reason, created_at, updated_at, expires_at
              FROM pending_interactions
             WHERE session_id = $1
               AND ($2 = '' OR tenant_id = $2)
               AND user_id = $3
               AND status IN ('pending', 'answered')
             ORDER BY created_at ASC
             LIMIT $4
            """,
            session_id,
            tenant_id or "",
            user_id or "",
            max(1, int(limit)),
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("pending interaction list failed (session=%s): %s", session_id, exc)
        return []
    out: list[dict[str, Any]] = []
    for row in rows or []:
        item = {str(k): row[k] for k in row.keys()}
        item["request"] = _decode(item.get("request"))
        item["decision"] = _decode(item.get("decision"))
        out.append(item)
    return out


async def list_open_for_user(
    *, tenant_id: str, user_id: str, limit: int = 20
) -> list[dict[str, Any]]:
    """**跨会话**列出这个人还等着的介入（M21 全局角标用）。

    与 `list_open` 的唯一区别是不按 `session_id` 过滤，其余谓词一字不改 ——
    `tenant_id` + `user_id` 那两条正是"只能看自己的票"的边界：少任何一条，
    这个端点就变成"列出所有人的待办"。`session_id` 进结果列，前端据此跳转。
    """
    if not user_id:
        return []
    try:
        pool = _pool()
        if pool is None:
            return []
        rows = await pool.fetch(
            """
            SELECT id, tenant_id, user_id, session_id, kind, status, run_id, turn_id,
                   tool_call_id, tool_name, args_hash, request, decision, reason,
                   created_at, updated_at, expires_at
              FROM pending_interactions
             WHERE user_id = $1
               AND ($2 = '' OR tenant_id = $2)
               AND status IN ('pending', 'answered')
             ORDER BY created_at ASC
             LIMIT $3
            """,
            user_id or "",
            tenant_id or "",
            max(1, int(limit)),
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("pending interaction user list failed (user=%s): %s", user_id, exc)
        return []
    out: list[dict[str, Any]] = []
    for row in rows or []:
        item = {str(k): row[k] for k in row.keys()}
        item["request"] = _decode(item.get("request"))
        item["decision"] = _decode(item.get("decision"))
        out.append(item)
    return out


#: 网关转投 SSE 用的 Redis 频道（订阅端见 internal/api/interaction_events_relay.go）。
#: 与 `subagent:events` 分开：两条通道的**受众不同**（子 Agent 进度 vs 等人答复），
#: 混在一条频道里以后谁都说不清"这条事件该给谁"。
INTERACTION_EVENTS_CHANNEL = "interaction:events"


async def _publish_pending_event(
    *,
    interaction_id: str,
    tenant_id: str,
    user_id: str,
    session_id: str,
    kind: str,
    request: dict[str, Any] | None,
) -> None:
    """把"多了一张待办"实时广播给网关（网关转投 SSE：会话通道 + 用户通道）。

    三条纪律：
    * **失败绝不影响落票** —— 票已经在 PG 里，通知只是让它更快被看见；
      前端角标还会按汇总端点轮询自愈（见 `list_open_for_user`）。
    * **不携带 `arguments`**：审批请求的参数可能含敏感内容，而这条事件会经 Redis 与
      SSE 到浏览器。只带"给人看的问题描述"（`question`），与列表端点不外泄 `args_hash`
      是同一条边界。
    * **没有 user_id 就不发**：用户通道靠它路由，缺了只能落到会话通道；宁可少一条推送，
      也不让一条无主事件被任何人认领。
    """
    if not user_id:
        return
    question = ""
    if isinstance(request, dict):
        question = str(request.get("question") or "")
    try:
        from app.redis_client import get_redis
        from app.redis_keys import rkey

        redis = await get_redis()
        if redis is None:
            return
        await redis.publish(
            rkey(INTERACTION_EVENTS_CHANNEL),
            json.dumps(
                {
                    "type": "interaction_pending",
                    "interaction_id": interaction_id,
                    "session_id": session_id,
                    "user_id": user_id,
                    "tenant_id": tenant_id,
                    "kind": kind,
                    "title": question[:200],
                },
                ensure_ascii=False,
            ),
        )
    except Exception as exc:  # noqa: BLE001 — 广播失败绝不能影响落票
        logger.warning(
            "interaction pending event publish failed (id=%s): %s", interaction_id, exc
        )


async def expire_stale(*, limit: int = 50) -> int:
    """把过期仍未完成的转成 `expired`，返回处理条数（reconciler 顺带扫）。

    为什么必须**转状态**而不是查询时用 `expires_at > now()` 过滤掉：一条 `answered` 的票
    过期后若不落状态，`ux_pending_interactions_open` 会一直为它保留"同一调用只有一个未完成"
    的名额 ⇒ 该会话重新问同一件事时 `create` 静默失败。过期是个**事件**，得留下痕迹。
    """
    try:
        pool = _pool()
        if pool is None:
            return 0
        result = await pool.execute(
            """
            UPDATE pending_interactions
               SET status = 'expired', updated_at = now()
             WHERE id IN (
                       SELECT id FROM pending_interactions
                        WHERE status IN ('pending', 'answered')
                          AND expires_at <= now()
                        ORDER BY expires_at ASC
                        LIMIT $1
                   )
            """,
            max(1, int(limit)),
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("pending interaction expiry sweep failed: %s", exc)
        return 0
    return _rowcount(result)
