"""billing_packages 按租户隔离：每个租户一套套餐目录。

**为什么**：SaaS 平台不再统一维护套餐目录，改由租户管理员在 /tenant/packages
维护自己的定价与权益；平台后台不再提供套餐管理页。

  - tenant_id = 0  历史平台套餐（保留为"平台默认目录"，租户未配置时可继续售卖）
  - tenant_id > 0  该租户自己的套餐

同一租户内 code 必须唯一；不同租户可以复用同一个 code（各自定价）。
迁移先清理历史重复 code（保留最早一行），再建 `(tenant_id, code)` 唯一索引。

Revision ID: 0009_packages_tenant
Revises: 0008_domains_unique
"""

from typing import Sequence, Union

from alembic import op

revision: str = "0009_packages_tenant"
down_revision: Union[str, Sequence[str], None] = "0008_domains_unique"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute(
        "ALTER TABLE public.billing_packages ADD COLUMN IF NOT EXISTS tenant_id smallint NOT NULL DEFAULT 0"
    )
    # 历史数据没有唯一约束，可能有重复 code；建索引前按 ctid 保留一行。
    op.execute(
        """
        DELETE FROM public.billing_packages a
              USING public.billing_packages b
              WHERE a.ctid > b.ctid
                AND a.tenant_id = b.tenant_id
                AND a.code = b.code
        """
    )
    # 旧的全局 code 唯一索引必须移除：不同租户要能复用同一个 code（各自定价）。
    op.execute("DROP INDEX IF EXISTS public.ux_billing_packages_code")
    op.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS ux_billing_packages_tenant_code "
        "ON public.billing_packages (tenant_id, code)"
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS public.ux_billing_packages_tenant_code")
    # 恢复全局唯一前先清理跨租户重复 code（保留 tenant_id 最小的一行）。
    op.execute(
        """
        DELETE FROM public.billing_packages a
              USING public.billing_packages b
              WHERE a.ctid > b.ctid
                AND a.code = b.code
        """
    )
    op.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS ux_billing_packages_code "
        "ON public.billing_packages (code)"
    )
    op.execute("ALTER TABLE public.billing_packages DROP COLUMN IF EXISTS tenant_id")
