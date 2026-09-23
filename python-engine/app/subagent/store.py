"""子 Agent 运行记录落库（``subagent_runs`` / ``subagent_run_steps``）。

设计要点（docs/subagent-design.md §3.2）：
* **两张表分离**：``subagent_runs`` 存 run 级元数据 + L1 精简摘要（要被父上下文读取），
  ``subagent_run_steps`` 存 L0 完整调用过程（审计/回放，永不进上下文）。
* **批量写**：steps 先在内存缓冲，达到阈值或结束时一次性 ``executemany``，
  避免"每个 token 一条 SQL"的写放大。
* **脱敏**：所有写库文本先过 :func:`app.subagent.redact.redact_text`。
* **缺表降级**：表未创建（老库）时只记 warning，不让子 Agent 因持久化失败而失败。
"""
from __future__ import annotations

import asyncio
import logging

from app.observability.metrics import (
    SUBAGENT_PERSIST_FAILED,
    SUBAGENT_RUN_STARTED,
    SUBAGENT_RUN_TERMINAL,
    SUBAGENT_RUN_UNFINALIZED,
)
from app.subagent.redact import redact_payload, redact_text

logger = logging.getLogger(__name__)

#: 终态白名单：指标标签只允许这几个值，未知状态归到 "other"。
#: 标签必须有界 —— 否则一次笔误（例如 status="Completed"）就会在 Prometheus 里
#: 分裂出新的时间序列，把"状态机是否收敛"的统计彻底打乱。
_TERMINAL_STATUSES = frozenset({"completed", "failed", "cancelled", "lost"})


def _status_label(status: str) -> str:
    value = (status or "").strip().lower()
    return value if value in _TERMINAL_STATUSES else "other"

# 单步内容入库上限（超出截断并置 truncated，与设计文档一致）
STEP_MAX_CHARS = 32 * 1024
# 缓冲阈值：达到即 flush
STEP_FLUSH_SIZE = 20
# L1 摘要上限（进入父上下文，必须短）
SUMMARY_MAX_CHARS = 4000

STEP_INSERT_SQL = """
INSERT INTO subagent_run_steps
    (run_id, seq, kind, role, tool_name, tool_call_id, content, truncated, input_tokens, output_tokens)
VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10)
ON CONFLICT (run_id, seq) DO NOTHING
"""

RUN_INSERT_SQL = """
INSERT INTO subagent_runs
    (id, root_session_id, turn_id, parent_run_id, depth, tenant_id, user_id, agent_id, profile_name,
     task, status, read_only, write_paths, started_at, created_at)
VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13::jsonb, now(), now())
ON CONFLICT (id) DO NOTHING
"""

#: 僵尸收口：只动超龄的 running 行（进程重启后它们的收尾代码再也不会执行）
#:
#: 参数类型必须是 int。此前写成 `($1 || ' hours')::interval`（`||` 是文本拼接，asyncpg
#: 因此期望 str）却传了 `int(max_age_hours)`，于是每次执行都抛
#: `invalid input for query argument $1: 2 (expected str, got int)`；
#: 而 `_reap_once` 会吞掉异常 —— 结果是**这个收口器从未成功执行过一次**，
#: DB 里 `status='running'` 的行永久残留、前端永远显示"运行中"。
#: `make_interval` 让参数类型显式且与调用方一致。
#:
#: "超龄"判定用**最后活跃时间**而不是 started_at：一个跑了很久但一直在产出步骤的 run
#: 不该被判为僵尸。`subagent_run_steps` 每步一行且带 created_at，它天然就是心跳 ——
#: 因此这里无需为心跳新增列（零迁移）。没有任何步骤时才退化为 started_at/created_at。
#:
#: 已知局限：子 Agent 若卡在**单个**长工具调用内，步骤时间也会停滞，可能被误判为 lost。
#: 那个场景由 wall 定时器（app/agent/subagent_runner.py 的 _watch_wall_budget）负责终止，
#: 不依赖本收口器。
REAP_STALE_SQL = """
WITH last_seen AS (
    SELECT r.id,
           COALESCE(
               (SELECT max(s.created_at) FROM subagent_run_steps s WHERE s.run_id = r.id),
               r.started_at,
               r.created_at
           ) AS seen_at
      FROM subagent_runs r
     WHERE r.status = 'running'
)
UPDATE subagent_runs r
   SET status = 'lost', finished_at = now(),
       error = 'unreaped: 进程未收尾（重启或超时）'
  FROM last_seen
 WHERE r.id = last_seen.id
   AND last_seen.seen_at < now() - make_interval(hours => $1::int)
"""

RUN_FINISH_SQL = """
UPDATE subagent_runs
   SET status = $2, summary = $3, summary_format = $4, artifacts = $5::jsonb,
       input_tokens = $6, output_tokens = $7, steps = $8, cost_cents = $9,
       redacted_count = $10, error = $11, finished_at = now()
 WHERE id = $1
"""


class SubagentRunStore:
    """``subagent_runs`` / ``subagent_run_steps`` 的写入端（引擎侧直连 PG）。"""

    def __init__(self, pool=None):
        self._pool = pool
        # run_id -> 待写入的 step 行（批量 flush）
        self._buffers: dict[str, list[tuple]] = {}
        self._seq: dict[str, int] = {}
        # run_id -> 脱敏命中数（finish 时汇总进 redacted_count）
        self._redacted_hits: dict[str, int] = {}
        self._lock = asyncio.Lock()
        self._broken = False  # 表缺失等不可恢复错误：本次进程内不再尝试
        #: 本实例已入 running 但尚未写终态的 run（用于 unfinalized 指标）。
        #: 用集合而不是裸计数器：终态可能被写两次（正常收尾 + 取消收尾竞争），
        #: 集合能保证同一个 run 只减一次，指标不会变成负数。
        self._unfinalized: set[str] = set()

    @property
    def available(self) -> bool:
        return self._pool is not None and not self._broken

    def _next_seq(self, run_id: str) -> int:
        self._seq[run_id] = self._seq.get(run_id, 0) + 1
        return self._seq[run_id]

    async def start_run(
        self,
        *,
        run_id: str,
        root_session_id: str,
        turn_id: str = "",
        parent_run_id: str = "",
        depth: int = 1,
        tenant_id: str = "",
        user_id: str = "",
        agent_id: str | None = None,
        profile_name: str = "",
        task: str = "",
        read_only: bool = False,
        write_paths: list[str] | None = None,
    ) -> None:
        """写入 run 起始记录（状态 running）。失败只告警，不阻断子 Agent。"""
        if not self.available:
            return
        import json

        safe_task, hits = redact_text(task)
        if hits:
            # 起始阶段命中：计数在 finish 时累加（这里仅脱敏入库）
            logger.info("subagent task redacted %d fragment(s)", hits)
        try:
            await self._pool.execute(
                RUN_INSERT_SQL,
                run_id,
                root_session_id,
                turn_id or None,
                parent_run_id or None,
                depth,
                tenant_id or None,
                user_id or None,
                agent_id,
                profile_name or None,
                safe_task,
                "running",
                read_only,
                json.dumps(write_paths or []),
            )
        except Exception as exc:  # noqa: BLE001
            self._degrade("start_run", exc)
            return
        self._mark_started(run_id)

    async def add_step(
        self,
        run_id: str,
        *,
        kind: str,
        content: str = "",
        role: str = "",
        tool_name: str = "",
        tool_call_id: str = "",
        input_tokens: int = 0,
        output_tokens: int = 0,
    ) -> None:
        """缓冲一条 L0 step（达到阈值即 flush）。"""
        if not self.available:
            return
        safe, hits = redact_text(content or "")
        truncated = False
        if len(safe) > STEP_MAX_CHARS:
            safe = safe[:STEP_MAX_CHARS]
            truncated = True
        row = (
            run_id,
            self._next_seq(run_id),
            kind,
            role or None,
            tool_name or None,
            tool_call_id or None,
            safe,
            truncated,
            int(input_tokens or 0),
            int(output_tokens or 0),
        )
        async with self._lock:
            buf = self._buffers.setdefault(run_id, [])
            buf.append(row)
            should_flush = len(buf) >= STEP_FLUSH_SIZE
        if hits:
            self._redacted_hits[run_id] = self._redacted_hits.get(run_id, 0) + hits
        if should_flush:
            await self.flush_steps(run_id)

    async def flush_steps(self, run_id: str) -> None:
        """把缓冲的 steps 批量写入。"""
        if not self.available:
            return
        async with self._lock:
            rows = self._buffers.pop(run_id, [])
        if not rows:
            return
        try:
            await self._pool.executemany(STEP_INSERT_SQL, rows)
        except Exception as exc:  # noqa: BLE001
            self._degrade("flush_steps", exc)

    async def finish_run(
        self,
        run_id: str,
        *,
        status: str,
        summary: str = "",
        summary_format: str = "markdown",
        artifacts: list[dict] | None = None,
        input_tokens: int = 0,
        output_tokens: int = 0,
        steps: int = 0,
        cost_cents: int = 0,
        error: str = "",
    ) -> None:
        """收尾：flush steps 并写 run 终态（含 L1 摘要与用量）。"""
        if not self.available:
            return
        import json

        await self.flush_steps(run_id)
        safe_summary, summary_hits = redact_text(summary or "")
        if len(safe_summary) > SUMMARY_MAX_CHARS:
            safe_summary = safe_summary[:SUMMARY_MAX_CHARS]
        safe_artifacts, artifact_hits = redact_payload(artifacts or [])
        safe_error, error_hits = redact_text(error or "")
        redacted = self._redacted_hits.pop(run_id, 0) + summary_hits + artifact_hits + error_hits
        try:
            await self._pool.execute(
                RUN_FINISH_SQL,
                run_id,
                status,
                safe_summary or None,
                summary_format,
                json.dumps(safe_artifacts, ensure_ascii=False),
                int(input_tokens or 0),
                int(output_tokens or 0),
                int(steps or 0),
                int(cost_cents or 0),
                int(redacted),
                safe_error or None,
            )
        except Exception as exc:  # noqa: BLE001
            self._degrade("finish_run", exc)
            return
        self._mark_finalized(run_id, status)

    async def reap_stale_runs(self, *, max_age_hours: int) -> int:
        """僵尸收口：把"还在 running 但早已超龄"的 run 标记为 ``lost``。

        为什么需要：进程重启/崩溃后，那些 run 的收尾代码再也不会执行 —— 数据库里会永久
        留一行 ``status='running'``，前端因此**永远显示"运行中"**（假活跃），也会污染
        "这个会话有多少子 Agent 在跑"这类判断。

        ``lost`` 与 ``cancelled`` 的语义差别很重要：前者是"没收到结果"，后者是"被停掉"。
        只动 ``status='running'`` 的行，绝不触碰任何终态记录。
        """
        if not self.available or max_age_hours <= 0:
            return 0
        try:
            result = await self._pool.execute(REAP_STALE_SQL, int(max_age_hours))
        except Exception as exc:  # noqa: BLE001
            self._degrade("reap_stale_runs", exc)
            return 0
        # asyncpg 的 execute 返回 "UPDATE n"；解析失败只影响日志，不影响结果
        count = 0
        try:
            count = int(str(result).split()[-1])
        except Exception:  # noqa: BLE001
            count = 0
        if count > 0:
            # 收口是**状态转移**（running → lost），必须计入指标 —— 否则
            # "收口器是否在干活"仍然只能靠查 DB。这里不递减 unfinalized：
            # 被收口的 run 由**上一个进程**启动，不在本实例的集合里。
            SUBAGENT_RUN_TERMINAL.labels(status="lost").inc(count)
        return count

    # ── 指标记账（只在权威写入成功后调用）──

    def _mark_started(self, run_id: str) -> None:
        SUBAGENT_RUN_STARTED.inc()
        self._unfinalized.add(run_id)
        SUBAGENT_RUN_UNFINALIZED.set(len(self._unfinalized))

    def _mark_finalized(self, run_id: str, status: str) -> None:
        SUBAGENT_RUN_TERMINAL.labels(status=_status_label(status)).inc()
        if run_id not in self._unfinalized:
            # 未曾由本实例登记（例如表缺失期间启动、或进程刚重启）：不减，
            # 否则 unfinalized 会变成负数，把一个可告警信号变成噪声。
            return
        self._unfinalized.discard(run_id)
        SUBAGENT_RUN_UNFINALIZED.set(len(self._unfinalized))

    # ── helpers ──

    def _degrade(self, where: str, exc: Exception) -> None:
        """表缺失等错误：记一次 warning 后本进程不再重试（避免刷日志）。"""
        message = str(exc)
        SUBAGENT_PERSIST_FAILED.labels(component="db", op=where).inc()
        if "does not exist" in message or "UndefinedTable" in message:
            if not self._broken:
                logger.warning(
                    "subagent 运行表不可用（%s）：请执行迁移 5e244b718fd1 —— 本次运行不落库",
                    where,
                )
            self._broken = True
        else:
            logger.warning("subagent 落库失败（%s）: %s", where, message[:200])

    def pending_step_count(self, run_id: str) -> int:
        return len(self._buffers.get(run_id, []))

    def step_count(self, run_id: str) -> int:
        return self._seq.get(run_id, 0)
