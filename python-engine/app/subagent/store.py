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
from typing import Any

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
#: A5（方案 04 §3）：`partial` = **部分完成**（有产物但不完整，例如撞上 wall 预算时）。
#: 它与 `failed` 的差别是"有没有可用产出"，与 `cancelled` 的差别是"谁停的"——三者都算终态。
#: 参照 Reasonix 的 7 阶段里的 `child_partial`。
_TERMINAL_STATUSES = frozenset({"completed", "failed", "cancelled", "lost", "partial"})


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

#: 重跑血缘单独写（而不是塞进上面的 INSERT）：老库还没有 `rerun_of` 列时，
#: **普通派发的落库不受影响** —— 只有"重跑"这一条新路径需要迁移
#: （f3a91c2d5e08）。把它并进 INSERT 会让未迁移的部署**所有**子 Agent 落库失败。
RERUN_OF_SQL = "UPDATE subagent_runs SET rerun_of = $2 WHERE id = $1"

#: S2：本次委派真正继承了父会话的多少条消息（审计计数，供前端显示
#: "这个子 Agent 看到了父的多少上下文"）。与 `rerun_of` 同理**单独写** ——
#: 未执行迁移（0004_subagent_inherited_messages）的库只会丢这个计数，
#: 不会让普通派发的 INSERT 因未知列整条失败。
INHERITED_MESSAGES_SQL = "UPDATE subagent_runs SET inherited_messages = $2 WHERE id = $1"

#: R5(b)：**续跑 / 分叉的血缘**。`resumed_from` = 被续跑的那个 run id，`resume_at_step` =
#: 分叉点（NULL = 续到底）。与上面两条同理由**单独写**：未执行迁移
#: （0008_subagent_resume_link）的库只会丢这两列，不会让普通派发的 INSERT 整条失败。
RESUMED_FROM_SQL = (
    "UPDATE subagent_runs SET resumed_from = $2, resume_at_step = $3 WHERE id = $1"
)

#: M15a：这条 run 属于**哪条任务线**（`root_run_id`）。同上一批的理由单独写（迁移
#: 0021_subagent_lineage_root）：未迁移的库只丢血缘，派发落库不受影响。
#: `AND root_run_id IS NULL` 是有意的：血缘一旦确定就不该再改，否则一条线中途换根
#: 会把已经计入的用量从新根下抹掉 —— 那是预算账上最坏的一类静默。
LINEAGE_SQL = (
    "UPDATE subagent_runs SET root_run_id = $2 WHERE id = $1 AND root_run_id IS NULL"
)

#: 解析新 run 的根。三条规则都写在这一个查询里，避免"读三次、在 Python 里拼"的竞态：
#:
#: 1. **续跑来源优先于父子**：续跑派生的新 run 同时也是当前回合的一棵新树，按父取根会把
#:    一条线按续跑次数切成若干段、每段各自有额度 —— 正是要修的缺陷。
#: 2. 来源行存在但它自己还没有根 ⇒ 取来源行的 id 开线，并靠 `claim_source` 把来源行**也**
#:    拉进这条线（它花过 token，漏掉就是少算）。
#: 3. 两者都解析不出（老数据 / 来源行不存在）⇒ 以自己为根。**不**回退成 NULL：闸门问
#:    "这条线花了多少"时必须有一个可查的键。
#:
#: 用相关子查询而不是 JOIN：`$2`/`$3` 常是空串（不是续跑、顶层委派），空串匹配不到行，
#: 标量子查询自然给 NULL，正好让 COALESCE 往下走 —— 换成 JOIN 会把整行丢掉。
ROOT_RESOLVE_SQL = """
WITH src AS (
    SELECT (SELECT root_run_id FROM subagent_runs WHERE id = $2) AS resumed_root,
           (SELECT id         FROM subagent_runs WHERE id = $2) AS resumed_row,
           (SELECT root_run_id FROM subagent_runs WHERE id = $3) AS parent_root,
           (SELECT id          FROM subagent_runs WHERE id = $3) AS parent_row
)
SELECT COALESCE(resumed_root, resumed_row, parent_root, parent_row, $1) AS root,
       CASE WHEN resumed_row IS NOT NULL AND resumed_root IS NULL
            THEN $2 ELSE NULL END AS claim_source
  FROM src
"""

#: 一条任务线累计花掉的 token。**直接对 run 行求和而不是另建一张累计表**：
#: `subagent_runs` 的行是每条 run 的**全程累计**且只在终态写一次，孙代的 token 记在孙自己
#: 那一行（`subagent` 工具回给父的只是文本，见 subagent_runner 的事件循环） ⇒ 求和不会双算，
#: 也不存在"累计表与明细行对不上"这种第二份真相。
#:
#: `tenant_id` 是**必填**过滤：这条 SQL 会被"拿一个 run id 就能问出用量"的调用用到，
#: 不按租户收就是跨租户读数。注意求和因此**不含** `tenant_id` 为 NULL 的行 —— 那是
#: 派发时就没有租户上下文的老数据，宁可少算（上限更早触发），不可跨租户多算。
LINEAGE_USAGE_SQL = """
SELECT COALESCE(sum(input_tokens + output_tokens), 0)::bigint AS tokens,
       count(*)::int                                           AS runs
  FROM subagent_runs
 WHERE root_run_id = $1 AND tenant_id = $2
"""

#: M26 会话线：**主 run 的回合用量**（`turns` 是网关按回合记账的权威表）。
#:
#: 为什么必须 join `users`：`turns` 只记 `user_id`、没有 `tenant_id`，不 join 就会把别的租户
#: 恰好同名的 `session_id` 的用量算进来 —— 与 M24 的 credits 求和同一个理由。
#: 为什么不用 `subagent_runs`：主 run 不写那张表（它就是"主"），它的用量只在这里。
SESSION_USAGE_SQL = """
SELECT COALESCE(sum(t.input_tokens + t.output_tokens), 0)::bigint AS tokens,
       count(*)::int                                              AS runs
  FROM turns t
  JOIN users u ON u.id = t.user_id
 WHERE t.session_id = $1 AND u.tenant_id = $2
"""

#: A5（方案 04 §3）：生命周期遥测**单独写**，与上面两条同理由 —— 未迁移
#: （0006_subagent_lifecycle）的库只会丢这几列，不会让子 Agent 的收尾 UPDATE 整条失败。
#:
#: 五列都是 **content-free** 的：`retryable` 是"值不值得重试"的判断，`output_bytes` 是体量
#: （不是全文），`validator_*` 是收尾校验的模式/结论/第几次尝试。刻意不含 prompt / 推理 /
#: 工具输出 / 路径 —— 这样它才能被转发进诊断链路而不泄漏会话内容。
#: `COALESCE` 让调用方可以只更新其中一部分（传 None = 保持原值）。
LIFECYCLE_SQL = """
UPDATE subagent_runs
   SET retryable = COALESCE($2, retryable),
       output_bytes = COALESCE($3, output_bytes),
       validator_mode = COALESCE($4, validator_mode),
       validator_outcome = COALESCE($5, validator_outcome),
       validator_attempt = COALESCE($6, validator_attempt),
       error_code = COALESCE($7, error_code)
 WHERE id = $1
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
#:
#: M15d：收口时**顺手把这一段花掉的 token 补上**。
#:
#: `subagent_runs.input_tokens/output_tokens` 只由 `finish_run` 写；进程崩在半路的那一段
#: 因此永远停在初值 0，而任务线账的 `used` 正是按 `root_run_id` 求和这张表
#: （见 `line_ledger.py` 的 `LINEAGE_USAGE_SQL`）⇒ **账上少算这一段实际花掉的钱**，
#: M15b 的闸门与 M15c 的待办都按这个偏小的数字判断，用户看到的"已花"也就是假的。
#:
#: 能重建的唯一依据是 `subagent_run_steps`：runner 每处理一条事件都写一行，并把这次 LLM
#: 调用的 `input_tokens/output_tokens` 记在该行上（累加口径与 run 行的终值同源，见
#: `subagent_runner.py` 里 `in_tokens += evt.input_tokens` 与同循环内的 `add_step`）。
#:
#: 这里是**赋值而不是相加**：这一列的既有值必然是初值 0（只有 `finish_run` 会写它，而写它
#: 就意味着已经离开 running）。相加会把同一批 token 数两遍。
#:
#: 已知偏差（方向仍是**少算**，与线账整体的 fail-open 取向一致）：步骤是**攒批 flush** 的（`STEP_FLUSH_SIZE`），
#: 崩溃时缓冲区里没落库的那几条一起丢了，补不回来。M18 把这条上界收窄了：runner 在每条
#: **带用量**的事件后 flush 一次（见 `subagent_runner.py` 的事件循环），而 token 就记在那些行上
#: ⇒ 已经算得出来的钱基本都在 PG 里，丢的是尾随其后的**无用量步骤行**（少的是"这段跑了多少步"
#: 与心跳时间，不是 token）。剩下那段"当前回合里模型调用还没返回"的花销**本来就不可能在这张表上**
#: —— 它由线账的在途预留（M18 起按"已花 + 投影余量"重述）覆盖，两道机制补的是同一段的两半。
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
),
doomed AS (
    SELECT ls.id,
           (SELECT COALESCE(SUM(s.input_tokens), 0)
              FROM subagent_run_steps s WHERE s.run_id = ls.id)::bigint  AS in_tokens,
           (SELECT COALESCE(SUM(s.output_tokens), 0)
              FROM subagent_run_steps s WHERE s.run_id = ls.id)::bigint  AS out_tokens,
           (SELECT COUNT(*) FROM subagent_run_steps s WHERE s.run_id = ls.id)::int AS step_rows
      FROM last_seen ls
     WHERE ls.seen_at < now() - make_interval(hours => $1::int)
)
UPDATE subagent_runs r
   SET status = 'lost', finished_at = now(),
       error = 'unreaped: 进程未收尾（重启或超时）',
       input_tokens = d.in_tokens,
       output_tokens = d.out_tokens,
       steps = CASE WHEN d.step_rows > COALESCE(r.steps, 0) THEN d.step_rows ELSE r.steps END
  FROM doomed d
 WHERE r.id = d.id
"""

#: M15d：给**已经被别人标成 lost** 的行补账。
#:
#: 为什么单独一条：网关的取消路径（`internal/api/subagent_cancel.go` 的 `markRunLost`）在
#: "没有任何实例认领这条 run"时**立刻**把行写成 `lost`，而它不写 token —— 上面那条收口的
#: 谓词只认 `status='running'`，因此那一行会**永久**停在 0，再也没人来补。
#: 把口径留在引擎这一处（两条 SQL 同一个求和形状），比让网关也抄一份求和 SQL 更可查。
#:
#: 谓词要求"当前 token 为 0 且步骤求和 > 0" ⇒ 天然幂等：补过一次就不再命中。
BACKFILL_LOST_USAGE_SQL = """
WITH zeroed AS (
    SELECT r.id
      FROM subagent_runs r
     WHERE r.status = 'lost'
       AND COALESCE(r.input_tokens, 0) + COALESCE(r.output_tokens, 0) = 0
),
filled AS (
    SELECT z.id,
           (SELECT COALESCE(SUM(s.input_tokens), 0)
              FROM subagent_run_steps s WHERE s.run_id = z.id)::bigint  AS in_tokens,
           (SELECT COALESCE(SUM(s.output_tokens), 0)
              FROM subagent_run_steps s WHERE s.run_id = z.id)::bigint  AS out_tokens,
           (SELECT COUNT(*) FROM subagent_run_steps s WHERE s.run_id = z.id)::int AS step_rows
      FROM zeroed z
)
UPDATE subagent_runs r
   SET input_tokens = f.in_tokens,
       output_tokens = f.out_tokens,
       steps = CASE WHEN f.step_rows > COALESCE(r.steps, 0) THEN f.step_rows ELSE r.steps END
  FROM filled f
 WHERE r.id = f.id
   AND f.in_tokens + f.out_tokens > 0
"""

RUN_FINISH_SQL = """
UPDATE subagent_runs
   SET status = $2, summary = $3, summary_format = $4, artifacts = $5::jsonb,
       input_tokens = $6, output_tokens = $7, steps = $8, cost_cents = $9,
       redacted_count = $10, error = $11, finished_at = now()
 WHERE id = $1
"""

#: `RUN_FINISH_SQL` 的 `cost_cents` **恒为 0**（没有任何调用方传过非零值），这是有意的口径，
#: 不是漏填：引擎侧没有价格表 —— 单价只存在于网关的 `internal/billing/billing.go`
#: （`BillingConfig.LLMCostPerToken/…PerOutput`，按 1000 token 计"额度"而不是按模型计金额）。
#: 在下游没有换算的情况下自己填一个数，写进去的就是**假钱**：这条线真正的成本由 Go 侧的
#: `billing_records.cost_cents`（`observer_enterprise.go` 随扣费事件写入）承担，两边各写一份
#: 必然对不上。而网关读子 Agent 运行列表时只选 `input_tokens/output_tokens/steps`
#: （见 `internal/api/subagent_handler.go:269`），并没有消费这一列 ⇒ 保持 0 不会让任何展示出错。
#: 要金额，去计费侧按那条线的实际模型名换算。


def _update_count(result: Any) -> int:
    """asyncpg 的 ``execute`` 返回 ``"UPDATE n"``；解析失败只影响日志，不影响结果。"""
    try:
        return int(str(result).split()[-1])
    except Exception:  # noqa: BLE001
        return 0


class SubagentRunStore:
    """``subagent_runs`` / ``subagent_run_steps`` 的写入端（引擎侧直连 PG）。"""

    def __init__(self, pool: Any = None) -> None:
        self._pool = pool
        # run_id -> 待写入的 step 行（批量 flush）
        self._buffers: dict[str, list[tuple[Any, ...]]] = {}
        self._seq: dict[str, int] = {}
        # run_id -> 脱敏命中数（finish 时汇总进 redacted_count）
        self._redacted_hits: dict[str, int] = {}
        self._lock = asyncio.Lock()
        self._broken = False  # 表缺失等不可恢复错误：本次进程内不再尝试
        #: 血缘读失败的**告警去重**标记（只影响日志，不影响 `_broken` 的降级判定）。
        self._lineage_warned = False
        #: M26 会话线：`session_usage` 的"只告警一次"旗标（与 `_lineage_warned` 同理由）。
        self._session_warned = False
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
        rerun_of: str = "",
        depth: int = 1,
        tenant_id: int = "",
        user_id: str = "",
        agent_id: str | None = None,
        profile_name: str = "",
        task: str = "",
        read_only: bool = False,
        write_paths: list[str] | None = None,
        inherited_messages: int = 0,
        resumed_from: str = "",
        resume_at_step: int | None = None,
    ) -> str:
        """写入 run 起始记录（状态 running）。失败只告警，不阻断子 Agent。

        返回值是这条 run 的**任务线根**（``root_run_id``，M15a/0021）：调用方要用它去问
        "这条线到今天花了多少"。落库不可用时返回**空串** —— 那是"不知道"，不是"以自己为根"：
        把不知道伪装成一条新线，闸门就会按每段重算额度。

        ``rerun_of`` 记录**重跑血缘**（由哪个 run 重跑而来），见
        app/tools/subagent_rerun.py；它单独写一条 UPDATE，因此未执行迁移的老库
        只会丢血缘，不会影响普通派发的落库。

        ``inherited_messages``（S2）是继承审计计数，**同样单独写一条 UPDATE**
        （迁移 ``0004_subagent_inherited_messages``）。0 = 未开启继承 ⇒ 不写，
        列保持 NULL —— 与"开启了但一条都没继承到"区分开。

        ``resumed_from`` / ``resume_at_step``（R5(b)）记录**续跑 / 分叉血缘**，
        同样单独写（迁移 ``0008_subagent_resume_link``）：`resume_at_step` 为 `None`
        表示"续到底"，有值表示"只继承了 `seq < N` 的步骤"。
        """
        if not self.available:
            return ""
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
            return ""
        if rerun_of:
            # 血缘写入失败**不能**影响这次运行本身：只告警（并给出迁移提示）
            try:
                await self._pool.execute(RERUN_OF_SQL, run_id, rerun_of)
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "subagent rerun_of 写入失败（run=%s, rerun_of=%s）：请执行迁移 "
                    "f3a91c2d5e08；本次运行的落库不受影响: %s",
                    run_id, rerun_of, str(exc)[:160],
                )
        if inherited_messages:
            # S2：继承审计计数（0 = 未开启继承，不写 —— 保持 NULL 与"继承 0 条"可分）。
            try:
                await self._pool.execute(
                    INHERITED_MESSAGES_SQL, run_id, int(inherited_messages)
                )
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "subagent inherited_messages 写入失败（run=%s）：请执行迁移 "
                    "0004_subagent_inherited_messages；本次运行的落库不受影响: %s",
                    run_id,
                    str(exc)[:160],
                )
        if resumed_from:
            # R5(b)：续跑 / 分叉的血缘（`resumed_from`）与分叉点（`resume_at_step`，
            # NULL = 续到底）。同样**单独写** —— 未迁移的库只丢这两列。
            try:
                await self._pool.execute(
                    RESUMED_FROM_SQL, run_id, resumed_from, resume_at_step
                )
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "subagent resumed_from 写入失败（run=%s, resumed_from=%s）：请执行迁移 "
                    "0008_subagent_resume_link；本次运行的落库不受影响: %s",
                    run_id,
                    resumed_from,
                    str(exc)[:160],
                )
        # M15a：这条 run 归属的任务线。必须在 `resumed_from` 之后写 —— 解析根的查询读的就是
        # 那一列；写在前面会让续跑出来的新 run 认不出自己的来源，额度按每段重算。
        root = run_id
        try:
            row = await self._pool.fetchrow(
                ROOT_RESOLVE_SQL, run_id, resumed_from, parent_run_id
            )
            root = str((row["root"] if row else "") or run_id)
            await self._pool.execute(LINEAGE_SQL, run_id, root)
            claim = (row["claim_source"] if row else None) or ""
            if claim:
                # 来源行自己还没有根 ⇒ 把它也拉进这条线，否则这条线的求和会漏掉它那一段。
                await self._pool.execute(LINEAGE_SQL, claim, root)
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "subagent root_run_id 写入失败（run=%s, resumed_from=%s, parent=%s）："
                "请执行迁移 0021_subagent_lineage_root；本次运行的落库不受影响: %s",
                run_id,
                resumed_from,
                parent_run_id,
                str(exc)[:160],
            )
        self._mark_started(run_id)
        # 解析失败时这里是 `run_id`（"自己一条线"）：库里的 root_run_id 因此可能是 NULL，
        # 于是这条线的求和会**少算**这一段 —— 与"按每段重算额度"（多算）相比是可接受的一侧。
        return root

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
        artifacts: list[dict[str, Any]] | None = None,
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

    async def mark_lifecycle(
        self,
        run_id: str,
        *,
        retryable: bool | None = None,
        output_bytes: int | None = None,
        validator_mode: str | None = None,
        validator_outcome: str | None = None,
        validator_attempt: int | None = None,
        error_code: str | None = None,
    ) -> None:
        """写生命周期遥测（A5）+ 结构化错误码（R3）。传 ``None`` = 保持原值。

        **全部是 content-free 的**：不含 prompt / 推理 / 工具输出 / 路径，因此可安全转发进诊断
        链路。失败只降级（与 `finish_run` 同语义 —— 遥测缺失不该影响对话行为）。

        `error_code` 与 `retryable` **同源**（都来自 `app/subagent/outcome.py`），不在这里另行判断。
        """
        if not self.available:
            return
        try:
            await self._pool.execute(
                LIFECYCLE_SQL,
                run_id,
                retryable,
                None if output_bytes is None else int(output_bytes),
                validator_mode,
                validator_outcome,
                None if validator_attempt is None else int(validator_attempt),
                error_code,
            )
        except Exception as exc:  # noqa: BLE001
            self._degrade("mark_lifecycle", exc)

    async def reap_stale_runs(self, *, max_age_hours: int) -> int:
        """僵尸收口：把"还在 running 但早已超龄"的 run 标记为 ``lost``，并补上它的用量。

        为什么需要：进程重启/崩溃后，那些 run 的收尾代码再也不会执行 —— 数据库里会永久
        留一行 ``status='running'``，前端因此**永远显示"运行中"**（假活跃），也会污染
        "这个会话有多少子 Agent 在跑"这类判断。

        ``lost`` 与 ``cancelled`` 的语义差别很重要：前者是"没收到结果"，后者是"被停掉"。
        只动 ``status='running'`` 的行，绝不触碰任何终态记录。

        M15d 之后还跑第二条语句（``BACKFILL_LOST_USAGE_SQL``）给**已经**是 lost 但 token
        仍为 0 的行补账 —— 网关的取消路径会立即标 lost 而不写用量，那些行落在上面那条
        的谓词之外，不补就永久少算。**返回值只含 running→lost 的转移数**：它是"收口器是否
        在干活"的指标口径，把补账的 UPDATE 数混进去会让计数虚高（补账是幂等的修复动作，
        不是状态转移）。补账条数单独记日志。
        """
        if not self.available or max_age_hours <= 0:
            return 0
        try:
            result = await self._pool.execute(REAP_STALE_SQL, int(max_age_hours))
        except Exception as exc:  # noqa: BLE001
            self._degrade("reap_stale_runs", exc)
            return 0
        count = _update_count(result)
        if count > 0:
            # 收口是**状态转移**（running → lost），必须计入指标 —— 否则
            # "收口器是否在干活"仍然只能靠查 DB。这里不递减 unfinalized：
            # 被收口的 run 由**上一个进程**启动，不在本实例的集合里。
            SUBAGENT_RUN_TERMINAL.labels(status="lost").inc(count)
        try:
            backfilled = _update_count(await self._pool.execute(BACKFILL_LOST_USAGE_SQL))
        except Exception as exc:  # noqa: BLE001
            # 与收口同一条降级路径：未迁移的库不该在这里抛新错误
            self._degrade("backfill_lost_usage", exc)
            backfilled = 0
        if backfilled > 0:
            logger.warning(
                "subagent reaper: backfilled usage on %d lost run(s) from step rows", backfilled
            )
            # Redis 的 `used` 镜像**不需要**在这里显式失效：它的 TTL 是
            # `line_ledger.USED_TTL_SECONDS`（60s），过期后第一次读会按新的 PG 求和回填
            # （NX 语义，不会复活已删的键）。也就是说闸门最多在一个 TTL 内继续按偏小的
            # 数字判断 —— 方向仍是"少算"，与线账整体的 fail-open 取向一致。
        return count

    async def lineage_usage(self, root_run_id: str, tenant_id: int) -> tuple[int, int] | None:
        """这条任务线（`root_run_id`）到今天为止累计花掉多少 token、跑过几条 run。

        返回 ``None`` = **问不出来**（表/列缺失、库不可用、或没有租户可比对）。
        刻意不用 ``_degrade``：那条路径会把整个 store 标成 broken，而这里只是一个读 ——
        让"未迁移的库"把子 Agent 落库整体关掉，代价远超收益。

        没有 `tenant_id` 时也返回 ``None`` 而不是 ``(0, 0)``：把"查不到"和"确实没花"混成
        一个值，会让 M15 的闸门在缺租户的派发上判成"还有额度"。调用方必须能区分这两件事。
        """
        if not self.available or not root_run_id or not tenant_id:
            return None
        try:
            row = await self._pool.fetchrow(LINEAGE_USAGE_SQL, root_run_id, tenant_id)
        except Exception as exc:  # noqa: BLE001
            if not self._lineage_warned:
                self._lineage_warned = True
                logger.warning(
                    "subagent lineage_usage 查询失败（此后不再重复告警）：请执行迁移 "
                    "0021_subagent_lineage_root: %s",
                    str(exc)[:160],
                )
            return None
        if row is None:
            return (0, 0)
        return int(row["tokens"] or 0), int(row["runs"] or 0)

    async def session_usage(self, session_id: str, tenant_id: int) -> tuple[int, int] | None:
        """这个**会话**到今天为止累计花掉多少 token、跑过几回合（M26 会话线）。

        与 `lineage_usage` 的分工：那条按 `root_run_id` 数**子 Agent 委派树**；这条按
        `session_id` 数**主 run 的回合**。主 run 自己不写 `subagent_runs`，它的用量只能从
        `turns`（网关按回合记账的权威表）拿 —— 这正是"主 run 不在线账之内"那个缺口。

        返回 ``None`` = **问不出来**（缺租户、库不可用、表缺失），与 `lineage_usage` 同口径：
        把"查不到"和"确实没花"混成一个值，会让闸门在缺租户的路径上判成"还有额度"。
        """
        if not self.available or not session_id or not tenant_id:
            return None
        try:
            row = await self._pool.fetchrow(SESSION_USAGE_SQL, session_id, tenant_id)
        except Exception as exc:  # noqa: BLE001
            if not self._session_warned:
                self._session_warned = True
                logger.warning(
                    "subagent session_usage 查询失败（此后不再重复告警）：%s", str(exc)[:160]
                )
            return None
        if row is None:
            return None
        return int(row["tokens"] or 0), int(row["runs"] or 0)

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
