"""system_settings(category, key) 唯一约束。

背景：`system_settings` 原先只有 `id` 主键，`(category, key)` 靠代码"先查后写"保证唯一；
并发写或手工 SQL 都可能插出重复键，读取侧取到哪一条取决于扫描顺序。

存量数据：若已有重复 `(category, key)`，建唯一索引会失败。这里 fail-fast 并列出冲突，
不静默删数据。

Revision ID: 0003_system_settings_key_unique
Revises: 0002_users_tenant_email_unique
"""

from typing import Sequence, Union

from alembic import op

revision: str = "0003_system_settings_key_unique"
down_revision: Union[str, Sequence[str], None] = "0002_users_tenant_email_unique"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    conn = op.get_bind()
    duplicates = conn.exec_driver_sql(
        """
        SELECT category, key, COUNT(*) AS n
          FROM public.system_settings
         WHERE category IS NOT NULL AND key IS NOT NULL
         GROUP BY category, key
        HAVING COUNT(*) > 1
         ORDER BY n DESC
         LIMIT 20
        """
    ).fetchall()

    if duplicates:
        detail = "; ".join(
            "category={} key={} x{}".format(row[0], row[1], row[2]) for row in duplicates
        )
        raise RuntimeError(
            "cannot create unique index ux_system_settings_category_key: existing "
            "duplicate (category, key) rows found -> {}. Resolve them and re-run "
            "`alembic upgrade head`.".format(detail)
        )

    op.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS ux_system_settings_category_key "
        "ON public.system_settings USING btree (category, key)"
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS public.ux_system_settings_category_key")
