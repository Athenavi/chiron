"""subagent runs and agents kind

Revision ID: 5e244b718fd1
Revises:
Create Date: 2026-09-21 18:34:06.169835

背景（重要）
------------
本仓库此前的迁移脚本（``migrations/versions/*.py``）已丢失，工作副本只留下
``__pycache__/*.pyc``；线上/本地库（``chiron0915``）由 ``migrations/sql/init.sql``
（72 张表的全量 DDL）建立，且**没有 alembic_version 记录**。

因此本迁移是 alembic 链的**新起点**（``down_revision = None``），以数据库现状为准，
只承载「子 Agent 子系统」所需的增量 DDL：

* ``agents.kind`` —— 区分可对话 Agent（``chat``）与子 Agent Profile（``subagent``）
* ``subagent_runs`` —— run 级元数据 + L1 精简摘要（唯一进入父上下文的内容）
* ``subagent_run_steps`` —— L0 完整调用过程（逐 step，供审计/回放）

部署指引
--------
* 既有库（已由 init.sql 建好历史表）：执行 ``alembic stamp 5e244b718fd1`` 对齐版本，
  或直接执行本迁移（DDL 幂等见下）。
* 全新库：执行 ``migrations/sql/init.sql`` 之后再 ``alembic upgrade head``。
* 本迁移的 DDL 与数据库现状逐字对应，重复执行不会破坏数据。
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


# revision identifiers, used by Alembic.
revision: str = '5e244b718fd1'
down_revision: Union[str, Sequence[str], None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema: 子 Agent 运行记录与 Profile 类型列。"""
    # ── 1. agents.kind：chat（可对话 Agent，默认）| subagent（子 Agent Profile）──
    op.execute("ALTER TABLE agents ADD COLUMN IF NOT EXISTS kind varchar(16) NOT NULL DEFAULT 'chat'")
    op.execute("CREATE INDEX IF NOT EXISTS agents_kind_idx ON agents (kind)")

    # ── 2. subagent_runs：run 级元数据 + L1 精简摘要 ──
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS subagent_runs (
            id                varchar(64)  NOT NULL PRIMARY KEY,
            root_session_id   varchar(128) NOT NULL,
            turn_id           varchar(64),
            parent_run_id     varchar(64),
            depth             integer      NOT NULL DEFAULT 1,
            tenant_id         varchar(36),
            user_id           varchar(36),
            agent_id          varchar(36) REFERENCES agents(id) ON DELETE SET NULL,
            profile_name      varchar(128),
            task              text         NOT NULL,
            status            varchar(16)  NOT NULL,
            summary           text,
            summary_format    varchar(16)  NOT NULL DEFAULT 'markdown',
            artifacts         jsonb        NOT NULL DEFAULT '[]'::jsonb,
            write_paths       jsonb        NOT NULL DEFAULT '[]'::jsonb,
            read_only         boolean      NOT NULL DEFAULT false,
            input_tokens      bigint       NOT NULL DEFAULT 0,
            output_tokens     bigint       NOT NULL DEFAULT 0,
            steps             integer      NOT NULL DEFAULT 0,
            cost_cents        integer      NOT NULL DEFAULT 0,
            redacted_count    integer      NOT NULL DEFAULT 0,
            error             text,
            started_at        timestamp,
            finished_at       timestamp,
            created_at        timestamp    NOT NULL DEFAULT now()
        )
        """
    )
    op.execute("CREATE INDEX IF NOT EXISTS subagent_runs_tree_idx ON subagent_runs (root_session_id, created_at)")
    op.execute("CREATE INDEX IF NOT EXISTS subagent_runs_parent_idx ON subagent_runs (parent_run_id)")
    op.execute("CREATE INDEX IF NOT EXISTS subagent_runs_turn_idx ON subagent_runs (turn_id)")
    op.execute("CREATE INDEX IF NOT EXISTS subagent_runs_tenant_idx ON subagent_runs (tenant_id, created_at)")

    # ── 3. subagent_run_steps：L0 完整调用过程（逐 step）──
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS subagent_run_steps (
            id            bigserial    PRIMARY KEY,
            run_id        varchar(64)  NOT NULL REFERENCES subagent_runs(id) ON DELETE CASCADE,
            seq           integer      NOT NULL,
            kind          varchar(16)  NOT NULL,
            role          varchar(16),
            tool_name     varchar(64),
            tool_call_id  varchar(128),
            content       text,
            truncated     boolean      NOT NULL DEFAULT false,
            input_tokens  integer      NOT NULL DEFAULT 0,
            output_tokens integer      NOT NULL DEFAULT 0,
            created_at    timestamp    NOT NULL DEFAULT now()
        )
        """
    )
    op.execute("CREATE UNIQUE INDEX IF NOT EXISTS subagent_run_steps_seq_idx ON subagent_run_steps (run_id, seq)")


def downgrade() -> None:
    """Downgrade schema: 移除子 Agent 运行记录与 Profile 类型列。"""
    op.execute("DROP TABLE IF EXISTS subagent_run_steps")
    op.execute("DROP TABLE IF EXISTS subagent_runs")
    op.execute("DROP INDEX IF EXISTS agents_kind_idx")
    op.execute("ALTER TABLE agents DROP COLUMN IF EXISTS kind")
