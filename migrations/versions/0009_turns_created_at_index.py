"""turns.created_at 的裸列索引：让保留清理从全表扫降为范围扫

Revision ID: 0009_turns_created_at_index
Revises: 0008_subagent_resume_link
Create Date: 2026-10-10

`internal/api/retention.go` 的 `purgeFinishedTurns` 每 6 小时执行一次清理：

    DELETE FROM turns
     WHERE status <> 'running'
       AND created_at < ((NOW() - make_interval(days => $1))::text)

`turns.created_at` 是 **VARCHAR(255)**（ORM 生成，写入值是 PG `NOW()` 的文本形式），
所以两侧都必须是文本才能比较。

**为什么补一条裸列索引**：既有 `ix_turns_status_created (status, created_at)`
的前导列是 `status`，而查询里 `status <> 'running'` 是不等式 —— 前导列无法做等值
定位，B-tree 基本用不上（PostgreSQL 没有 skip scan）。`ix_turns_created_at`
以 `created_at` 为前导列，可支持 `created_at < X` 的范围扫描。

**为什么不是表达式索引**：早先的写法是 `created_at::timestamptz < …`，在表达式上
建索引会报 `functions in index expression must be marked IMMUTABLE` ——
`text::timestamptz` 依赖 `TimeZone` GUC，只是 STABLE。因此改为「右侧也转成文本、
左侧保持裸列」，这样既能走这条索引，也避免了强转。

``IF NOT EXISTS``：与既有迁移风格一致。

> 新增迁移后请复核 README 的「数据库迁移」段：网关/引擎启动时会把
> `migrations/versions` 的 head 与库中 `alembic_version` 比对（
> `internal/db/schema_version.go` 的 `CheckSchemaVersion`），库未 stamp 到本
> revision 会直接拒绝启动（`ALLOW_SCHEMA_DRIFT=true` 可放行）。
"""

from typing import Sequence, Union

from alembic import op

revision: str = "0009_turns_created_at_index"
down_revision: Union[str, Sequence[str], None] = "0008_subagent_resume_link"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS ix_turns_created_at
            ON public.turns USING btree (created_at)
        """
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS public.ix_turns_created_at")
