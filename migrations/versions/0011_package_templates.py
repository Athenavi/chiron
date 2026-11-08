"""平台套餐模板：在历史三档之外补充 team / enterprise / trial。

这些行是 `tenant_id = 0` 的平台模板：租户后台新建套餐时可以一键预填，
租户保存时写入自己的 `tenant_id`，不会改动模板本身。

`ON CONFLICT (tenant_id, code) DO NOTHING`：运营改过模板价格后重新执行迁移
不会覆盖；只补缺失的模板。

Revision ID: 0011_package_templates
Revises: 0010_usage_indexes
"""

from typing import Sequence, Union

from alembic import op

revision: str = "0011_package_templates"
down_revision: Union[str, Sequence[str], None] = "0010_usage_indexes"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


# (code, name, credits, price_cents, currency, duration_days, sort_order)
_TEMPLATES: tuple[tuple[str, str, int, int, str, int, int], ...] = (
    ("trial", "试用包", 500, 0, "CNY", 14, 5),
    ("team", "团队版", 30000, 30000, "CNY", 90, 40),
    ("enterprise", "企业版", 100000, 100000, "CNY", 180, 50),
)


def upgrade() -> None:
    for code, name, credits, price_cents, currency, duration_days, sort_order in _TEMPLATES:
        op.execute(
            f"""
            INSERT INTO public.billing_packages
                (id, tenant_id, code, name, credits, price_cents, currency,
                 duration_days, allowed_models, enabled, sort_order,
                 created_at, updated_at)
            VALUES
                (gen_random_uuid()::text, 0, '{code}', '{name}', {credits},
                 {price_cents}, '{currency}', {duration_days},
                 ARRAY[]::text[], TRUE, {sort_order}, now(), now())
            ON CONFLICT (tenant_id, code) DO NOTHING
            """
        )


def downgrade() -> None:
    for code, *_ in _TEMPLATES:
        op.execute(
            f"DELETE FROM public.billing_packages WHERE tenant_id = 0 AND code = '{code}'"
        )
