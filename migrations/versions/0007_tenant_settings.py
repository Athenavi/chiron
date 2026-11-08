"""system_settings 增加 tenant_id：按租户保存一套配置（支付渠道等）。

**为什么**：SaaS 里每个租户应使用自己的支付商户号/密钥，而不是全平台共用一套。
`system_settings` 原本只有 (category, key)，只能表达"全局一份"。加 tenant_id 后：

  - tenant_id = 0  全局默认（平台后台维护，作为未单独配置租户的回退）
  - tenant_id > 0  该租户自己的配置（租户管理员在 /tenant 后台维护）

唯一索引同步改为 (tenant_id, category, key)，避免同一租户同一键出现多行。

Revision ID: 0007_tenant_settings
Revises: 0006_turns_context
"""

from typing import Sequence, Union

from alembic import op

revision: str = "0007_tenant_settings"
down_revision: Union[str, Sequence[str], None] = "0006_turns_context"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute(
        "ALTER TABLE public.system_settings ADD COLUMN IF NOT EXISTS tenant_id smallint NOT NULL DEFAULT 0"
    )
    # 历史数据在无唯一约束时可能产生重复键（应用侧是逐键 upsert，理论上不会，
    # 但并发首次写入可能）。建唯一索引前先清掉重复，保留 ctid 最大的一行。
    op.execute(
        """
        DELETE FROM public.system_settings a
              USING public.system_settings b
              WHERE a.ctid < b.ctid
                AND a.tenant_id = b.tenant_id
                AND a.category = b.category
                AND a.key = b.key
        """
    )
    op.execute("DROP INDEX IF EXISTS public.ux_system_settings_category_key")
    op.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS ux_system_settings_tenant_category_key "
        "ON public.system_settings (tenant_id, category, key)"
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS public.ux_system_settings_tenant_category_key")
    op.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS ux_system_settings_category_key "
        "ON public.system_settings (category, key)"
    )
    op.execute("ALTER TABLE public.system_settings DROP COLUMN IF EXISTS tenant_id")
