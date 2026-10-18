"""人工介入现场表 pending_interactions

Revision ID: 0018_pending_interactions
Revises: 0017_conversation_shares_expires_backfill
Create Date: 2026-10-18

背景（缺陷「人工介入闭环」，方案 1A+1B；那份核心机制剖析文档**不在仓库里**，
结论全部写在下面）：审批 / `ask_user` 的现场**只存在于进程内存**里 ——
`AgentRuntime._pending_approvals` 的 Future 加一条 Redis 决策键
（`app/agent/runtime.py:_await_approval`）。于是：

* 发起确认的那个进程一死，待确认的那一步就**无人知晓**：用户看不到"这里在等你"，
  续跑侧也只知道"缺一个结果"（`agent_runs.replay_pending`），不知道**为什么**缺；
* M6（`149f6a7` / `1644e0d`）把无人值守回合的空等改成立即拒绝，止住了 reconciler 被
  300s 闸门钉住，但代价是**用户仍然看不见**"这一步需要你确认"，只能看到一句拒绝。

本表把"正在等人"变成**可查询、可跨进程、可到期**的落库现场：
`request` 存要展示给用户的东西，`decision` 存用户给的回答，`status` 走
`pending → answered → consumed`。它是 PG（本项目唯一的持久存储）里"待人工输入"的唯一真相源；
Redis 决策键继续做它的老本行（同一次运行内的跨副本唤醒），两者职责不同、不互相替换。

列约定：

* ``kind``：``approval`` / ``question``。两类的"等人"形状相同（一个问题 + 一个答复），
  差别只在答复的载荷，因此共用一张表；分流由 ``kind`` 在读侧完成。
* ``tool_call_id`` / ``tool_name`` / ``args_hash``：``args_hash`` 由
  ``app/agent/tool_policy.args_hash`` 计算（**归一化后**的参数哈希）。它是续跑侧的**唯一**
  认票依据 —— 新一轮 run 的模型可能把同一个调用重排成不同的 ``tool_call_id``，
  但 (工具名, 参数) 一致才代表"用户批准的正是这一次"。绑定参数而不绑 id 是有意的：
  参数变了就是另一件事，必须重新问（``consume_grant`` 因此按 ``args_hash`` 匹配）。
* ``run_id`` / ``turn_id``：诊断用。``run_id`` 存的是发起这次确认的 run **归属租约
  （run_token）**，不是 ``agent_runs.id`` —— 引擎手里只有前者，而"是谁问的"只需要能对上
  Redis 租约。（``agent_runs.run_token`` 同为 varchar(64)，这里保持一致。）
* ``status``：``pending``（等人）/ ``answered``（人已给出、等回合来取）/
  ``consumed``（已被某一回合取走，**单次消费**）/ ``expired``（到期，见下）。
  ``answered → consumed`` 必须原子（见 ``interactions.consume_grant``）：两个回合同时续跑时，
  批准只能生效一次。
  **没有** ``cancelled``：目前没有任何路径会产生"用户主动取消某一条介入"这个动作
  （取消会话/回合走的是 ``agent_runs`` 的终态），先留一个用不上的取值只会让读侧多一个
  无法测试的分支。
* ``expires_at``：**默认 24h**，与 ``agent_runs`` 的冷恢复窗口（``COLD_RESUME_WINDOW_SECONDS``）
  对齐 —— 超过那个窗口快照本身已判 ``abandoned``，一条还"可以批准"的票留着只会让用户批准
  一件早已无法续跑的事。到期由 ``expire_stale`` 收尾成 ``expired``（可见的失败，不是静默丢弃）。

**没有外键**：``session_id`` / ``tenant_id`` 与 ``agent_runs`` 一样是**逻辑**引用。会话删除时
这些行随 ``session_id`` 一起失去意义；加 FK 会让网关的会话删除路径多一个必须存在、且按
``session_id``（非主键）反查的约束，代价大于收益。

**部分索引**（``WHERE status IN ('pending','answered')``）：这张表的行**永不删除**（审计需要），
活跃占比却极低。把谓词写进索引而不是查询条件，既让"列出该会话待办"是常数级，也让
``ux_pending_interactions_open`` 只约束"同一会话同一工具调用**最多一个未完成的人工介入**"，
历史行不受影响。

``awaiting_input``（``app/agent/checkpoint.STATUS_AWAITING_INPUT``）是 ``agent_runs.status``
的一个**新取值**，本迁移**不**改 ``ux_agent_runs_session_active`` 的谓词 —— 这是刻意的：
那个部分唯一索引定义的就是"同一会话最多一个活跃 run"，而"等人"恰恰要求把会话**释放**出来
（用户可能先聊别的、也可能永远不回来）。把它排除在活跃集合之外，同时意味着 reconciler 的
扫描（只认 ``running`` / ``checkpointed``）**在定义上**不可能接管一个等人的回合：
不需要额外的分支，也不会有"巡检代替用户点了批准"。``agent_runs.status`` 是无 CHECK 约束的
varchar(16)，``awaiting_input``（15 字符）直接可用，因此这里无需任何 ALTER。

时间列用 ``timestamptz``：与 ``0003_agent_runs`` 同样的理由（路线图 L4-3 的方向是把时间列
提升为 ``timestamptz``，新表直接对齐目标）。

``IF NOT EXISTS``：与既有迁移风格一致（某些环境若已手工建表则不覆盖）。
"""

from typing import Sequence, Union

from alembic import op

revision: str = "0018_pending_interactions"
down_revision: Union[str, Sequence[str], None] = "0017_conversation_shares_expires_backfill"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS pending_interactions (
            id           varchar(36) NOT NULL,
            tenant_id    varchar(36),
            user_id      varchar(36),
            session_id   varchar(128),
            run_id       varchar(64),
            turn_id      varchar(36),
            kind         varchar(16) NOT NULL,
            tool_call_id varchar(128) NOT NULL,
            tool_name    varchar(128) NOT NULL DEFAULT '',
            args_hash    varchar(64) NOT NULL DEFAULT '',
            request      jsonb,
            status       varchar(16) NOT NULL DEFAULT 'pending',
            decided_by   varchar(36),
            decision     jsonb,
            reason       text,
            created_at   timestamptz NOT NULL DEFAULT now(),
            updated_at   timestamptz NOT NULL DEFAULT now(),
            expires_at   timestamptz NOT NULL DEFAULT (now() + interval '24 hours'),
            PRIMARY KEY (id)
        )
        """
    )
    # 同一会话同一工具调用最多一个未完成的人工介入（历史行不参与约束）
    op.execute(
        """
        CREATE UNIQUE INDEX IF NOT EXISTS ux_pending_interactions_open
            ON pending_interactions (session_id, tool_call_id)
         WHERE status IN ('pending', 'answered')
        """
    )
    # 会话侧"有哪些事在等我"（网关列表路由）
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS ix_pending_interactions_session_open
            ON pending_interactions (session_id, created_at)
         WHERE status IN ('pending', 'answered')
        """
    )
    # 续跑侧认票：按 (会话, 参数哈希) 找 answered 的授权
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS ix_pending_interactions_grant
            ON pending_interactions (session_id, args_hash)
         WHERE status = 'answered'
        """
    )
    # 到期清扫
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS ix_pending_interactions_expiry
            ON pending_interactions (status, expires_at)
         WHERE status IN ('pending', 'answered')
        """
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS ix_pending_interactions_expiry")
    op.execute("DROP INDEX IF EXISTS ix_pending_interactions_grant")
    op.execute("DROP INDEX IF EXISTS ix_pending_interactions_session_open")
    op.execute("DROP INDEX IF EXISTS ux_pending_interactions_open")
    op.execute("DROP TABLE IF EXISTS pending_interactions")
