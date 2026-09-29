"""对话 run 现场 checkpoint 表 agent_runs

Revision ID: 0003_agent_runs
Revises: 0002_ent_chaos_experiments
Create Date: 2026-09-29

背景（方案 01 §3.1、评审 01 §一）：实例故障时该 run **直接中断**，用户重试只能整轮重跑
（`python-engine/app/run_registry.py` 的文件头写明"只登记归属，不做现场状态的持久化/迁移"）。
本表让"整轮重跑"降级为"从最近一次 checkpoint 续跑"：已完成的回合与工具调用不重放。

**与 `workflow_instances` 同构**：workflow 侧已有节点级 checkpoint（`executor.load_checkpoint`
+ `engine.run_workflow(resume_state=…, resume_done=…)`），本表把同一形状搬到对话 run。

列约定：

* ``status``：running / checkpointed / resuming / completed / failed / cancelled / abandoned
  —— 迁移条件见方案 01 §3.1；``running → checkpointed`` 的判定**不在引擎内部**（故障进程
  无法自证死亡），由接管方依据 run 租约过期 + 会话运行锁可获取来判定；
* ``checkpoint``：回合边界快照（jsonb）。形状与"只存尾部窗口 + 既有压缩摘要"的约定见
  评审 01 §1.2；`done_tools` / `replay_pending` 的精确判定依据见 §1.3；
* ``run_token``：与 Redis 归属租约（`engine:run:{sid}`）比对用，接管时换新值；
* ``instance_id``：原主标识，仅诊断用（**不**做现场内存迁移，见方案 01 §9）。

**时间列用 `timestamptz`（与本库既有 142 列 `timestamp` 不一致）**：这是刻意的 ——
路线图 L4-3 的方向就是把时间列提升为 `timestamptz`，新表直接对齐目标，避免再迁一次。
存量列的迁移依据（会话时区语义）见该条。

**`ux_agent_runs_session_active`**：同一会话同时只允许一行活跃记录。它是**防脑裂的数据库
兜底** —— 多实例同时接管同一会话时，只有一个能插入 `resuming` 行，其余拿唯一冲突后跳过，
因此 reconciler 不需要额外的分布式限流（评审 01 §1.4）。

``IF NOT EXISTS``：与既有迁移风格一致（某些环境若已手工建表则不覆盖）。
"""

from typing import Sequence, Union

from alembic import op

revision: str = "0003_agent_runs"
down_revision: Union[str, Sequence[str], None] = "0002_ent_chaos_experiments"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS agent_runs (
            id            varchar(36) NOT NULL,
            tenant_id     varchar(36),
            session_id    varchar(128),
            turn_id       varchar(36),
            run_token     varchar(64),
            status        varchar(16) NOT NULL DEFAULT 'running',
            checkpoint    jsonb,
            checkpoint_at timestamptz,
            instance_id   varchar(128),
            created_at    timestamptz NOT NULL DEFAULT now(),
            updated_at    timestamptz NOT NULL DEFAULT now(),
            PRIMARY KEY (id)
        )
        """
    )
    # 防脑裂兜底：同一会话只允许一行活跃记录（多实例同时接管时由唯一索引裁决）
    op.execute(
        """
        CREATE UNIQUE INDEX IF NOT EXISTS ux_agent_runs_session_active
            ON agent_runs (session_id)
         WHERE status IN ('running', 'checkpointed', 'resuming')
        """
    )
    # reconciler 的扫描路径：按 status + checkpoint_at 找"该被接管"的行
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS ix_agent_runs_status_cp
            ON agent_runs (status, checkpoint_at)
        """
    )
    # 按会话回查（用户重试时读最近一次 checkpoint）
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS ix_agent_runs_session_updated
            ON agent_runs (session_id, updated_at DESC)
        """
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS ix_agent_runs_session_updated")
    op.execute("DROP INDEX IF EXISTS ix_agent_runs_status_cp")
    op.execute("DROP INDEX IF EXISTS ux_agent_runs_session_active")
    op.execute("DROP TABLE IF EXISTS agent_runs")
