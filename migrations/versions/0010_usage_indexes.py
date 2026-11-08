"""索引优化：system_settings 按分类查租户、billing_packages 按租户列套餐。

- `TenantIDsWithConfig` 的查询是 `WHERE category = $1 AND tenant_id > 0`；
  现有唯一索引 `(tenant_id, category, key)` 的第一列是 tenant_id，用不上它，
  租户规模上来后每次支付回调都会退化成顺序扫描。
- `billing_packages` 的租户后台列表/模板查询都是 `WHERE tenant_id = ...`，
  追加 `(tenant_id, enabled, sort_order)` 覆盖过滤与排序。

Revision ID: 0010_usage_indexes
Revises: 0009_packages_tenant
"""

from typing import Sequence, Union

from alembic import op

revision: str = "0010_usage_indexes"
down_revision: Union[str, Sequence[str], None] = "0009_packages_tenant"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_system_settings_category_tenant "
        "ON public.system_settings (category, tenant_id)"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_billing_packages_tenant_enabled "
        "ON public.billing_packages (tenant_id, enabled, sort_order)"
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS public.ix_billing_packages_tenant_enabled")
    op.execute("DROP INDEX IF EXISTS public.ix_system_settings_category_tenant")
