"""按 run 的用量结算水位 ``run_usage_settlements``（M19，论文 §5.2「计费缺口」）

Revision ID: 0026_run_usage_settlements
Revises: 0025_kb_index_engine
Create Date: 2026-10-19

缺口：崩溃 / 自动接管那一段**从不经过网关**（``python-engine/app/main.py`` 的
``_auto_resume_runner``），因此没有 ``DeductTokens``、没有 ``billing_records``、
没有 ``TrueUpTenantQuota``；而网关只按终态 ``done`` 事件计费，崩溃段根本没有
``done`` ⇒ 那一份用量永久不入账。

**为什么需要一张表，而不是拿 ``agent_runs.checkpoint`` 反推。**
``checkpoint.usage`` 是引擎侧**唯一**的 run 级用量真相（M15e：每回合末赋值式覆写
全程累计）。但"已经入账多少"只有网关知道（``turns`` 与 ``billing_records``），
而这两处都不是按 run 组织的。本表只存**结算水位**（这个 run 已经处理到哪个总量），
不存第二份用量真相 —— 与 0022 ``run_lineage_ledger``「只存控制态」同一条取舍。

对账公式（网关 ``internal/api/run_usage_reconciler.go``）：:

    total      = checkpoint.usage（全程累计）
    billed     = 该 run 的 turn 集合在 turns / billing_records 里的用量之和
    delta      = total - max(billed, settled_input + settled_output)
    delta > 0 → 补偿 delta（扣余额 + 写 billing_records）→ settled = total

**水位用"总量"而不是"增量"**：同一行会被反复对账（续跑会继续抬高 ``usage``），
增量累加在水位被人工修正、或网关补写了 ``turns`` 之后就会重复收钱；总量比较
天然收敛。

``status`` 只描述**补偿动作**的状态（``settled`` / ``failed`` / ``skipped``），
不是 run 的状态 —— run 的状态仍以 ``agent_runs.status`` 为唯一真相。
``skipped`` 用于对账主动放弃的情形（``turn_ids_truncated``、缺少 ``user_id``、
超出 ``turns`` 保留期等），并保留 ``last_error`` 让下一轮/运维看得见。

``tenant_id`` / ``user_id`` / ``session_id`` 是**结算时的快照**：``agent_runs``
的行可能被后续提交复用或清理，结算记录必须能独立回答"这笔钱是谁的"。
"""

from typing import Sequence, Union

from alembic import op

revision: str = "0026_run_usage_settlements"
down_revision: Union[str, Sequence[str], None] = "0025_kb_index_engine"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS run_usage_settlements (
            run_id          varchar(36) NOT NULL,
            tenant_id       varchar(36),
            user_id         varchar(36),
            session_id      varchar(128),
            settled_input   bigint NOT NULL DEFAULT 0,
            settled_output  bigint NOT NULL DEFAULT 0,
            settled_cached  bigint NOT NULL DEFAULT 0,
            checkpoint_at   timestamptz,
            status          varchar(16) NOT NULL DEFAULT 'pending',
            attempts        integer NOT NULL DEFAULT 0,
            last_error      text,
            created_at      timestamptz NOT NULL DEFAULT now(),
            updated_at      timestamptz NOT NULL DEFAULT now(),
            PRIMARY KEY (run_id)
        )
        """
    )
    # 失败重试的扫描路径：只挑需要再次尝试的行。`settled` 行永远不进这个索引。
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS ix_run_usage_settlements_retry
            ON run_usage_settlements (status, updated_at)
         WHERE status IN ('pending', 'failed')
        """
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS ix_run_usage_settlements_retry")
    op.execute("DROP TABLE IF EXISTS run_usage_settlements")
