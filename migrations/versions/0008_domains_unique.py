"""domains 一对一约束：一个域名只属于一个租户，一个租户只有一个域名。

**为什么**：域名决定登录入口（`resolveTenantByHost` 用 `lower(domain)=Host` 反查
tenant_id）。此前表上没有唯一约束，于是可能出现：

  - 同一域名被两个租户同时绑定 → 反查结果取决于执行计划，登录被导向随机租户；
  - 一个租户绑多个域名 → 与"单一租户绑定单一域名"的产品约定不符。

迁移会先清理历史重复数据（每组保留"已验证优先、创建更早、ctid 更小"的一条），
再建两个部分唯一索引。domain 为空的行不参与（历史占位行不阻塞）。

Revision ID: 0008_domains_unique
Revises: 0007_tenant_settings
"""

from typing import Sequence, Union

from alembic import op

revision: str = "0008_domains_unique"
down_revision: Union[str, Sequence[str], None] = "0007_tenant_settings"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # 1) 同一域名（忽略大小写）只保留一条。
    op.execute(
        """
        WITH ranked AS (
            SELECT ctid,
                   row_number() OVER (
                       PARTITION BY lower(domain)
                       ORDER BY COALESCE(verified, false) DESC,
                                created_at ASC NULLS LAST,
                                ctid
                   ) AS rn
              FROM public.domains
             WHERE domain IS NOT NULL AND domain <> ''
        )
        DELETE FROM public.domains d
              USING ranked r
              WHERE d.ctid = r.ctid AND r.rn > 1
        """
    )
    # 2) 同一租户只保留一个域名。
    op.execute(
        """
        WITH ranked AS (
            SELECT ctid,
                   row_number() OVER (
                       PARTITION BY tenant_id
                       ORDER BY COALESCE(verified, false) DESC,
                                created_at ASC NULLS LAST,
                                ctid
                   ) AS rn
              FROM public.domains
             WHERE domain IS NOT NULL AND domain <> ''
        )
        DELETE FROM public.domains d
              USING ranked r
              WHERE d.ctid = r.ctid AND r.rn > 1
        """
    )
    op.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS ux_domains_domain_lower "
        "ON public.domains (lower(domain)) "
        "WHERE domain IS NOT NULL AND domain <> ''"
    )
    op.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS ux_domains_tenant_single "
        "ON public.domains (tenant_id) "
        "WHERE domain IS NOT NULL AND domain <> ''"
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS public.ux_domains_tenant_single")
    op.execute("DROP INDEX IF EXISTS public.ux_domains_domain_lower")
