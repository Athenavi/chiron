"""run 现场 checkpoint（方案 01 §3.1、评审 01 §一）—— **C1 批 2：只写不读**。

本批只做「建表 + 回合末落盘」，**不接恢复路径**：这样即使写入有问题也不影响对话本身
（写入失败只降级「恢复粒度」，与 workflow 的 `persist_checkpoint` 同一语义）。
恢复（用户重试 / reconciler）在批 3 接入。

三条形状 / 边界约定（评审 01 §1.2、§1.3、§1.5）：

* ``messages``：**尾部窗口（按 token 预算）+ 既有压缩摘要**；下限保证最近 2 个完整回合 ——
  否则 `assistant(tool_calls)` / `tool` 配对会断在快照边上（那正是
  `_ensure_valid_tool_sequence` 要修的情形，不该由 checkpoint 制造）；
* ``done_tools`` / ``replay_pending``：前者是跳过依据；后者是"已开始未结束"的**精确**判定结果
  —— 依据 `tool_calls` 的两段式写入（执行前 INSERT 空 output、执行后 UPDATE，见
  `internal/session/manager.go`），因此不必采用"保守视为已完成"的策略；
* **TTL 两档**：热续跑 **1h**（与 SSE 重放窗口 `sseEventsTTL` 对齐 —— 只有在此窗口内
  "用户无感"才成立）/ 冷恢复 **24h**（允许续跑但不承诺事件补齐），超窗 `abandoned`（可见）。

状态机：`running` / `checkpointed` / `resuming` / `completed` / `failed` / `cancelled` / `abandoned`。
其中 `running → checkpointed` 的判定**不在引擎内部**（故障进程无法自证死亡），由接管方依据
run 租约过期 + 会话运行锁可获取来判定（方案 01 §3.1）。
第八个取值 `awaiting_input`（M7，见 `load_awaiting` / `claim_awaiting`）**刻意不属于活跃集合**。
"""

from __future__ import annotations

import calendar
import json
import logging
import time
import uuid
from typing import Any, TypedDict, cast

from app.tools.context import to_tenant_int

logger = logging.getLogger(__name__)

#: 热续跑窗口（秒）：与 SSE 重放窗口对齐（`internal/broadcast/hub.go` 的 sseEventsTTL = 1h）。
#: 只有在此窗口内，"缺口由 Last-Event-ID 补发 ⇒ 用户无感"才成立。
HOT_RESUME_WINDOW_SECONDS = 3600
#: 冷恢复窗口（秒）：允许续跑但**不承诺事件补齐**（前端需重拉消息列表）。
COLD_RESUME_WINDOW_SECONDS = 24 * 3600

STATUS_RUNNING = "running"
STATUS_CHECKPOINTED = "checkpointed"
STATUS_RESUMING = "resuming"
STATUS_COMPLETED = "completed"
STATUS_FAILED = "failed"
STATUS_CANCELLED = "cancelled"
STATUS_ABANDONED = "abandoned"

#: M7（人工介入闭环）：这个 run **停在"等一个人回答"**上，不是崩溃、也不是结束。
#:
#: **刻意不进 `ACTIVE_STATUSES`**，三个后果都是要的：
#:
#: * `ux_agent_runs_session_active` 的谓词不含它 ⇒ 会话被**释放**，用户可以接着聊别的，
#:   也可以永远不回来（"等人"不该把整个会话锁住）；
#: * reconciler 的扫描只认 `running` / `checkpointed` ⇒ 巡检**在定义上**不可能接管一个
#:   等人的回合 —— 不会出现"机器代替用户点了批准"；
#: * `load()` / `claim()` 的活跃集合过滤也不含它 ⇒ 一个"早已没人等"的陈旧挂起快照不会
#:   被后面某条无关消息复活（那正是批 3 加状态过滤要防的事）。
#:
#: 读取它需要走 `load_awaiting` + `claim_awaiting`：**读与交还同一个动作**，读完就变成
#: `resuming`（此后按普通活跃 run 处理），因此不存在"第二次读到同一份挂起现场"。
STATUS_AWAITING_INPUT = "awaiting_input"

#: 活跃状态 —— 与唯一索引 `ux_agent_runs_session_active` 的谓词一致
ACTIVE_STATUSES: tuple[str, ...] = (STATUS_RUNNING, STATUS_CHECKPOINTED, STATUS_RESUMING)

#: 快照里 messages 的字节上限（超过则只留尾部窗口）
SNAPSHOT_MESSAGES_MAX_BYTES = 256 * 1024
#: 快照里 messages 的**条数**上限。
#:
#: 必须同时有条数上限：只有字节上限时，"200 条小消息"总字节数远小于 256KB，于是快照等于
#: 整段 —— 那就失去了"尾部窗口"的意义（也会让 checkpoint 行随会话线性膨胀）。
#: 待 C4（模型窗口表）落地后，这里改为按窗口比例计算。
SNAPSHOT_MAX_TAIL_MESSAGES = 40

#: A2（方案 04 批次 1）：快照的 schema 版本。
#:
#: **为什么需要它**：没有版本时，"旧引擎读到新格式快照"是**静默**的 —— 旧代码按老字段名取值，
#: 取不到就落到默认值，现场被悄悄读歪。有了版本，读取侧可以**明确拒绝**：宁可不续跑，
#: 也不带着被误读的现场续跑（对齐 Reasonix 的 turn ledger —— 未知版本 `leave it untouched`）。
#:
#: 本常量引入**之前**写下的快照没有该字段，按 **v1** 认 —— 这是显式承认既有形态，不是猜测。
SNAPSHOT_SCHEMA_VERSION = 1
#: 快照至少保留的条数（保证最近若干条完整，避免把 `assistant(tool_calls)` 与其
#: `tool` 结果切到快照两侧）
SNAPSHOT_MIN_TAIL_MESSAGES = 4

#: M19：一条 run 链最多跟踪多少个网关 turn id。
#:
#: 正常一链是"1 个提交 turn + 若干次无人值守续跑（没有 turn）"，远小于这个数；真到了
#: 上限说明这条链被重试过很多次，再往快照里堆 id 只会让 jsonb 无界增长。截断时**必须**
#: 置 `turn_ids_truncated`：对账依赖"已计费 turn 的完整集合"，少一个就会把已计费的钱
#: 再补一遍（多收）。
MAX_TRACKED_TURN_IDS = 512


class RunCheckpoint(TypedDict, total=False):
    """回合边界快照。字段语义见模块文档与方案 01 §3.1。"""

    #: A2：写入时的 schema 版本（见 `SNAPSHOT_SCHEMA_VERSION`）。读取侧据此拒绝未知版本。
    schema_version: int
    turn_index: int
    messages: list[dict[str, Any]]
    done_tools: list[str]
    replay_pending: list[str]
    pending: dict[str, Any]
    turn_id: str
    #: M19：**这一条 run 链涉及过的网关 turn id**（本次 run + 历次续跑继承的）。
    #: 网关的对账 job 靠它把快照里的全程用量与 `turns` 里已经计过费的回合对应起来 ——
    #: 崩溃/接管那一段没有自己的 turn，但它的用量必须算进这条链的账。
    #: 只存 id、不存用量：用量真相仍在 `usage` 一格，避免同一笔钱有两个数。
    turn_ids: list[str]
    #: `turn_ids` 达到上限被截断（只保留最近 N 个）。对账看到它就**跳过**该 run：
    #: 漏掉的历史 turn 会让"已计费量"偏小，照补就是**多收**用户的钱。
    turn_ids_truncated: bool
    #: 本 run 的**全程累计**用量（`input_tokens` / `output_tokens` / `cached_tokens` /
    #: `tool_steps` / `wall_seconds`）。M15e 起它是 run 级预算跨续跑的唯一依据：接管那一段
    #: 读回它当起点（见 `ResumeState.input_tokens`），否则"每次接管白送一份 per-run 额度"。
    #: 每回合末由**花钱的那一段**整体覆写（赋值，不相加），所以它最多陈旧到上一个回合边界。
    usage: dict[str, int]
    last_event_id: str
    instance_id: str
    saved_at: str
    #: C1 批 3+：**重建 AgentTask 所需的最小元信息**（`llm_config` / `workbench_context` /
    #: `system_prompt` / `user_id` / `max_turns` …）。有了它，reconciler 才能自动续跑 ——
    #: 这些配置只存在于**提交请求**里，不存进快照就没有第二个稳定来源。
    task_meta: dict[str, Any]


def _pool() -> Any:
    """取引擎的 PG 池（`app/db.py` 的兼容层：直连或经网关的统一客户端）。"""
    from app.db import get_pool

    return get_pool()


def merge_turn_ids(
    existing: Any,
    current: str,
    *,
    already_truncated: bool = False,
) -> tuple[list[str], bool]:
    """把当前 turn id 并入快照的 `turn_ids`，返回 `(列表, 是否截断)`。

    * 去重、保序（历史在前），空值忽略 —— 无人值守的续跑段本来就没有网关 turn；
    * `already_truncated` 一旦为真就**保持为真**：后续再合并也不会把丢掉的历史找回来，
      对账因此不会误以为这份集合是完整的；
    * 超上限时保留**最近**的若干条并置截断标记。保最近而不是保最早：`turns` 表按
      created_at 有保留期，早先的 turn 本来就可能已被清理。
    """
    out: list[str] = []
    for item in list(existing or []):
        value = str(item or "").strip()
        if value and value not in out:
            out.append(value)
    value = str(current or "").strip()
    if value and value not in out:
        out.append(value)
    truncated = bool(already_truncated)
    if len(out) > MAX_TRACKED_TURN_IDS:
        out = out[-MAX_TRACKED_TURN_IDS:]
        truncated = True
    return out, truncated


def build_snapshot(
    *,
    messages: list[dict[str, Any]],
    turn_index: int,
    turn_id: str = "",
    turn_ids: list[str] | None = None,
    turn_ids_truncated: bool = False,
    done_tools: list[str] | None = None,
    replay_pending: list[str] | None = None,
    usage: dict[str, int] | None = None,
    last_event_id: str = "",
    instance_id: str = "",
    task_meta: dict[str, Any] | None = None,
    max_tail_messages: int = SNAPSHOT_MAX_TAIL_MESSAGES,
    min_tail_messages: int = SNAPSHOT_MIN_TAIL_MESSAGES,
) -> RunCheckpoint:
    """构造快照：**尾部窗口 + 既有压缩摘要**（评审 01 §1.2）。

    为什么不是整段：会话消息已有 PG `messages` 表与 Redis 缓存，checkpoint 只需"能重放的最小现场"。

    两道上限**都要**：条数上限保证"尾部窗口"在任何消息尺寸下都成立（只有字节上限时，
    很多条小消息会让快照等于整段）；字节上限挡住单条巨型消息。

    `min_tail_messages` 是**下限**：快照不能把一个回合切一半 —— 保证最近若干条完整保留，
    避免 `assistant(tool_calls)` 与它的 `tool` 结果被分到快照两侧。
    """
    limit = max(min_tail_messages, max_tail_messages)
    tail: list[dict[str, Any]] = []
    used = 0
    for message in reversed(messages):
        size = len(json.dumps(message, ensure_ascii=False).encode("utf-8"))
        if tail and (len(tail) >= limit or used + size > SNAPSHOT_MESSAGES_MAX_BYTES):
            break
        tail.append(message)
        used += size
    tail.reverse()
    if not tail:
        tail = messages[-min_tail_messages:]

    return RunCheckpoint(
        schema_version=SNAPSHOT_SCHEMA_VERSION,
        turn_index=turn_index,
        messages=tail,
        done_tools=list(done_tools or []),
        replay_pending=list(replay_pending or []),
        usage=dict(usage or {}),
        turn_id=turn_id,
        turn_ids=list(turn_ids or []),
        turn_ids_truncated=bool(turn_ids_truncated),
        last_event_id=last_event_id,
        instance_id=instance_id,
        saved_at=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        task_meta=dict(task_meta or {}),
    )


async def save(
    *,
    tenant_id: str,
    session_id: str,
    checkpoint: RunCheckpoint,
    run_token: str = "",
    instance_id: str = "",
) -> bool:
    """回合末落盘（**异步、失败只降级恢复粒度**）。

    返回是否写入成功 —— 调用方**不该**因为它失败而改变对话行为（与 workflow 的
    `persist_checkpoint` 同一语义：失败只影响"恢复粒度"）。

    写入策略：先 UPDATE 活跃行，没有再 INSERT。并发下 INSERT 可能撞
    `ux_agent_runs_session_active`（同一会话的另一实例刚写了）→ 重试一次 UPDATE。
    """
    if not session_id:
        return False

    payload = json.dumps(checkpoint, ensure_ascii=False)
    now_token = run_token or uuid.uuid4().hex
    try:
        pool = _pool()
        if pool is None:
            return False

        updated = await pool.execute(
            """
            UPDATE agent_runs
               SET checkpoint = $3::jsonb,
                   checkpoint_at = now(),
                   turn_id = $4,
                   run_token = COALESCE(NULLIF($5, ''), run_token),
                   instance_id = COALESCE(NULLIF($6, ''), instance_id),
                   updated_at = now()
             WHERE session_id = $1
               AND ($2 = 0 OR tenant_id = $2)
               AND status IN ('running', 'checkpointed', 'resuming')
            """,
            session_id,
            to_tenant_int(tenant_id),
            payload,
            str(checkpoint.get("turn_id", "") or ""),
            now_token,
            instance_id,
        )
        if _rowcount(updated) > 0:
            return True

        await pool.execute(
            """
            INSERT INTO agent_runs
                (id, tenant_id, session_id, turn_id, run_token, status,
                 checkpoint, checkpoint_at, instance_id, created_at, updated_at)
            VALUES ($1, $2, $3, $4, $5, 'running', $6::jsonb, now(), $7, now(), now())
            """,
            uuid.uuid4().hex,
            to_tenant_int(tenant_id),
            session_id,
            str(checkpoint.get("turn_id", "") or "") or None,
            now_token,
            payload,
            instance_id or None,
        )
        return True
    except Exception as exc:  # noqa: BLE001 — 落盘失败不改变对话行为
        logger.warning("checkpoint save failed (session=%s): %s", session_id, exc)
        return False


async def load(*, tenant_id: str, session_id: str) -> RunCheckpoint | None:
    """读最近一次**活跃 run** 的 checkpoint。

    只认 `ACTIVE_STATUSES`（running/checkpointed/resuming）：终态行（completed/failed/
    cancelled/abandoned）代表**已经画过句号**的 run，它们的快照只是历史残留 ——
    不加这层过滤时，下一条消息可能从一个"早已正常结束"的陈旧尾部快照续跑
    （终态行因保存缺终态写入而停留 running 时同样中招）。
    """
    if not session_id:
        return None
    try:
        pool = _pool()
        if pool is None:
            return None
        row = await pool.fetchrow(
            """
            SELECT checkpoint, checkpoint_at, status
              FROM agent_runs
             WHERE session_id = $1 AND ($2 = 0 OR tenant_id = $2)
               AND status IN ('running', 'checkpointed', 'resuming')
             ORDER BY updated_at DESC
             LIMIT 1
            """,
            session_id,
            to_tenant_int(tenant_id),
        )
        if row is None:
            return None
        raw = row["checkpoint"]
        if raw is None:
            return None
        parsed = json.loads(raw) if isinstance(raw, str) else dict(raw)
        return cast(RunCheckpoint, parsed)
    except Exception as exc:  # noqa: BLE001
        logger.warning("checkpoint load failed (session=%s): %s", session_id, exc)
        return None


async def mark(
    *,
    tenant_id: str,
    session_id: str,
    status: str,
    run_token: str = "",
) -> bool:
    """迁移状态（`completed` / `failed` / `cancelled` / `abandoned` …）。

    终态行会被移出活跃集合（唯一索引随之释放），因此同一会话可以开始新 run。

    `run_token` 是**防踩踏**条件：终态写入来自 run 自己的收尾路径，若这期间同会话已有
    新 run 接管（save 会把行上的 token 换成新 run 的），带旧 token 的 UPDATE 必须
    0 行命中 —— 否则旧 run 的迟到收尾会把**正在运行的新 run** 标成终态、
    释放唯一索引，等于第三个 run 也能进来（双 runtime 竞态）。
    reconciler/abandoned 等"旁观者"路径不带 token，保持原有语义。
    """
    if not session_id:
        return False
    try:
        pool = _pool()
        if pool is None:
            return False
        result = await pool.execute(
            """
            UPDATE agent_runs
               SET status = $3, updated_at = now()
             WHERE session_id = $1 AND ($2 = 0 OR tenant_id = $2)
               AND ($4 = '' OR run_token = $4)
               AND status IN ('running', 'checkpointed', 'resuming')
            """,
            session_id,
            to_tenant_int(tenant_id),
            status,
            run_token or "",
        )
        return _rowcount(result) > 0
    except Exception as exc:  # noqa: BLE001
        logger.warning("checkpoint mark failed (session=%s): %s", session_id, exc)
        return False


async def claim(
    *,
    tenant_id: str,
    session_id: str,
    expected_run_token: str,
    new_run_token: str,
) -> bool:
    """**原子抢占**一个僵尸 run（C1 批 3+ 的接管 CAS）。返回是否抢到。

    为什么必须 CAS 而不是"先查后改"：多个实例的巡检会**同时**看到同一行，"先查后改"下它们
    各自的 UPDATE 都会成功 → 同一个 run 被跑两遍（重复副作用 + 双倍 token）。条件里带上
    `expected_run_token` 之后，只有第一个 UPDATE 能命中，其余 rowcount = 0。

    （方案原文写"抗接管风暴靠 `ux_agent_runs_session_active` 唯一索引"；那条索引防的是
    "同一会话出现两行活跃"，而接管是**同一行改状态**，索引不参与 —— 所以这里用条件更新达到
    同一目的，语义更直接。）
    """
    if not session_id or not new_run_token:
        return False
    try:
        pool = _pool()
        if pool is None:
            return False
        result = await pool.execute(
            """
            UPDATE agent_runs
               SET status = $4, run_token = $5, updated_at = now()
             WHERE session_id = $1
               AND ($2 = 0 OR tenant_id = $2)
               AND status IN ('running', 'checkpointed')
               AND ($3 = '' OR run_token = $3)
            """,
            session_id,
            to_tenant_int(tenant_id),
            expected_run_token or "",
            STATUS_RESUMING,
            new_run_token,
        )
        return _rowcount(result) > 0
    except Exception as exc:  # noqa: BLE001
        logger.warning("checkpoint claim failed (session=%s): %s", session_id, exc)
        return False


async def load_awaiting(
    *, tenant_id: str, session_id: str
) -> dict[str, Any] | None:
    """M7：读那个"正在等人"的 run 的现场（`{checkpoint, run_token}`）；没有则 None。

    `load()` 只认活跃状态，因此挂起中的现场需要一个专门的读入口 —— 而它**必须**与
    `claim_awaiting` 配对使用：只读不交还会留下一个"谁都不认领、但下次又能被读到"的现场。
    """
    if not session_id:
        return None
    try:
        pool = _pool()
        if pool is None:
            return None
        row = await pool.fetchrow(
            """
            SELECT checkpoint, run_token
              FROM agent_runs
             WHERE session_id = $1 AND ($2 = 0 OR tenant_id = $2)
               AND status = 'awaiting_input'
             ORDER BY updated_at DESC
             LIMIT 1
            """,
            session_id,
            to_tenant_int(tenant_id),
        )
        if row is None or row["checkpoint"] is None:
            return None
        raw = row["checkpoint"]
        parsed = json.loads(raw) if isinstance(raw, str) else dict(raw)
        if not isinstance(parsed, dict):
            return None
        return {"checkpoint": cast(RunCheckpoint, parsed), "run_token": str(row["run_token"] or "")}
    except Exception as exc:  # noqa: BLE001
        logger.warning("awaiting-input load failed (session=%s): %s", session_id, exc)
        return None


async def claim_awaiting(
    *, tenant_id: str, session_id: str, new_run_token: str
) -> bool:
    """把挂起的 run **交还给即将续跑的这一轮**（`awaiting_input → resuming`）。

    为什么在这里做 CAS 而不是让调用方"先读后改"：同一会话的挂起现场只该被**一个**回合接手。
    条件更新只有第一个能命中；其余 0 行。

    它同时是**防脑裂的兜底**：`resuming` 属于活跃集合，若此时会话上已有另一个活跃行
    （用户一边等批准、一边又开了新回合且锁失效了），这条 UPDATE 会撞
    `ux_agent_runs_session_active` ⇒ 抛异常 ⇒ 返回 False ⇒ 调用方按"没能续跑"走普通路径。
    也就是说：**宁可重来一遍上下文，也不会让两个 run 共用一份挂起现场。**
    """
    if not session_id or not new_run_token:
        return False
    try:
        pool = _pool()
        if pool is None:
            return False
        result = await pool.execute(
            """
            UPDATE agent_runs
               SET status = 'resuming', run_token = $3, updated_at = now()
             WHERE session_id = $1 AND ($2 = 0 OR tenant_id = $2)
               AND status = 'awaiting_input'
            """,
            session_id,
            to_tenant_int(tenant_id),
            new_run_token,
        )
        return _rowcount(result) > 0
    except Exception as exc:  # noqa: BLE001 — 唯一索引冲突是**预期**结果，不是故障
        logger.info("awaiting-input claim rejected (session=%s): %s", session_id, str(exc)[:160])
        return False


async def release_awaiting(
    *, tenant_id: str, session_id: str, run_token: str = ""
) -> bool:
    """M7：把挂起现场**作废**（`awaiting_input → abandoned`）—— 读完之后唯一的其他出路。

    `load()` / `claim()` / `mark()` 的谓词都不含 `awaiting_input`，所以超窗或快照损坏时
    **不能**用 `mark()` 收尾（0 行命中 = 那一行会永远停在 `awaiting_input`，每一条后续消息
    都再白读一次）。可见地放弃、并且真的把它移出"可被接手"的集合，是批 3 那条"降级必须显式"
    的同一原则。
    """
    if not session_id:
        return False
    try:
        pool = _pool()
        if pool is None:
            return False
        result = await pool.execute(
            """
            UPDATE agent_runs
               SET status = 'abandoned', updated_at = now()
             WHERE session_id = $1 AND ($2 = 0 OR tenant_id = $2)
               AND status = 'awaiting_input'
               AND ($3 = '' OR run_token = $3)
            """,
            session_id,
            to_tenant_int(tenant_id),
            run_token or "",
        )
        return _rowcount(result) > 0
    except Exception as exc:  # noqa: BLE001
        logger.warning("awaiting-input release failed (session=%s): %s", session_id, exc)
        return False


async def pending_tool_calls(
    *, session_id: str, limit: int = 50
) -> list[dict[str, Any]]:
    """"已开始未结束"的工具调用（C1 批 3 的 `replay_pending` 判定依据）。

    判据来自 `tool_calls` 表的**两段式写入**（Go 侧 `SessionManager.SaveToolCall` 先 INSERT
    空 `output`，`UpdateToolCall` 再写结果）：`output = ''` 就是"执行前落了库、执行后没回来"
    —— 即中断在中间的那一次。因此不必保守地把整个回合当成未完成（那会重复执行已成功的调用）。

    只按 `session_id` 过滤：`tool_calls` 表没有 `tenant_id` 列，而 session 本身已归属租户
    （会话 id 全局唯一）。
    """
    if not session_id:
        return []
    try:
        pool = _pool()
        if pool is None:
            return []
        rows = await pool.fetch(
            """
            SELECT id, tool_name, turn_id
              FROM tool_calls
             WHERE session_id = $1
               AND COALESCE(output, '') = ''
             ORDER BY created_at DESC
             LIMIT $2
            """,
            session_id,
            max(1, int(limit)),
        )
    except Exception as exc:  # noqa: BLE001 — 判定失败按"没有待重放"处理（不阻断续跑）
        logger.warning("pending tool calls lookup failed (session=%s): %s", session_id, exc)
        return []

    out: list[dict[str, Any]] = []
    for row in rows or []:
        try:
            out.append(
                {
                    "id": str(row["id"]),
                    "tool_name": str(row["tool_name"] or ""),
                    "turn_id": str(row["turn_id"] or ""),
                }
            )
        except (KeyError, TypeError):
            continue
    return out


def resume_window(checkpoint: RunCheckpoint) -> str:
    """按快照的保存时间判定恢复窗口：`hot` / `cold` / `abandoned`。

    这是"用户无感"承诺的**前提**：热窗口内缺口由 SSE 重放补齐；冷窗口内只能续跑、
    不能承诺事件补齐（评审 01 §1.5）。
    """
    saved_at = str(checkpoint.get("saved_at", "") or "")
    if not saved_at:
        return "cold"
    try:
        # 必须用 `calendar.timegm`（把 struct_time 当 **UTC**）而不是 `time.mktime`
        # —— 后者按**本地时区**解释，于是 UTC+8 的机器上 age 会偏差 8 小时，
        # "热窗口"判定直接错位（这个 bug 在测试里才暴露）。
        saved = calendar.timegm(time.strptime(saved_at, "%Y-%m-%dT%H:%M:%SZ"))
    except ValueError:
        return "cold"
    age = time.time() - saved
    if age <= HOT_RESUME_WINDOW_SECONDS:
        return "hot"
    if age <= COLD_RESUME_WINDOW_SECONDS:
        return "cold"
    return "abandoned"


def _rowcount(result: Any) -> int:
    """从 asyncpg 的 `UPDATE n` 状态串里取行数（统一客户端也返回同一形状）。"""
    text = str(result or "")
    if text.startswith("UPDATE "):
        try:
            return int(text.split(" ", 1)[1])
        except (IndexError, ValueError):
            return 0
    return 0
