"""修复人机验证配置保存：ent_captcha_config 缺少 tenant_id 唯一约束。

UpdateConfig 使用 `INSERT ... ON CONFLICT (tenant_id)`，但基线表只有
`tenant_id` 外键、没有唯一索引，PostgreSQL 直接报
`there is no unique or exclusion constraint matching the ON CONFLICT specification`
（SQLSTATE 42P10），导致保存必失败（503 service temporarily unavailable）。

单租户单行是本表的设计前提（见 captcha_handler.go 注释），补唯一索引即可。

Revision ID: 0014_captcha_config_unique_tenant
Revises: 0013_billing_alignment
"""

from typing import Sequence, Union

from alembic import op

revision: str = "0014_captcha_config_unique_tenant"
down_revision: Union[str, Sequence[str], None] = "0013_billing_alignment"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS ux_ent_captcha_config_tenant_id "
        "ON public.ent_captcha_config (tenant_id);"
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS public.ux_ent_captcha_config_tenant_id;")
