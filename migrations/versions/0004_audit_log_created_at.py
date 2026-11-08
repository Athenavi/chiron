"""audit_logs.created_at 补默认值并回填 NULL。

**为什么需要**：操作审计页按 `created_at` 做时间窗过滤（`WHERE created_at >= $2 AND
created_at < $3`），而写入方（`internal/db/audit_sink.go` 的批量 INSERT、
`ent_webhook_dispatcher.go` 的投递记录）都没有写这一列，基线里的列也没有 DEFAULT。
结果是每一行 created_at 都是 NULL：`NULL >= $2` 恒为 false，**审计页永远查不到任何记录**。

这与 `tenants.created_at` / `domains.created_at` 是同一类问题（迁移 0003 已修过那两张表）。
这里补默认值并回填存量 NULL（回填成迁移时刻 —— 真实时间不可知，但不回填就永远读不出来）。

Revision ID: 0004_audit_log_created_at
Revises: 0003_uuid_defaults_and_timestamps
"""

from typing import Sequence, Union

from alembic import op

revision: str = "0004_audit_log_created_at"
down_revision: Union[str, Sequence[str], None] = "0003_uuid_defaults_and_timestamps"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute("ALTER TABLE public.audit_logs ALTER COLUMN created_at SET DEFAULT now()")
    op.execute("UPDATE public.audit_logs SET created_at = now() WHERE created_at IS NULL")


def downgrade() -> None:
    # 只撤销默认值；已回填的时间戳不回滚（回滚成 NULL 只会让审计页再次空空如也）。
    op.execute("ALTER TABLE public.audit_logs ALTER COLUMN created_at DROP DEFAULT")
