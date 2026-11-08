"""计费口径对齐：usage_events 增加 credits_charged；billing_records 增加 cached_tokens。

背景（P1-9）：`usage_events.cost_cents` 历史上被写入的是**用户余额扣减 credits**，
而 `billing_records.cost_cents` 是**企业成本（分）**。两者同名不同义，前端把前者
当“费用（元）”展示，导致口径错误。

本迁移把两者拆开：
- `usage_events.credits_charged`：这一轮实际扣减的 credits（免费额度内为 NULL）；
- `usage_events.cost_cents`：保留给引擎侧真实成本（当前可空）；
- `billing_records.cached_tokens`：与 usage_events 对齐，便于逐项对账。

Revision ID: 0013_billing_alignment
Revises: 0012_payment_refunds
"""

from typing import Sequence, Union

from alembic import op

revision: str = "0013_billing_alignment"
down_revision: Union[str, Sequence[str], None] = "0012_payment_refunds"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute(
        "ALTER TABLE public.usage_events "
        "ADD COLUMN IF NOT EXISTS credits_charged integer;"
    )
    op.execute(
        "ALTER TABLE public.billing_records "
        "ADD COLUMN IF NOT EXISTS cached_tokens bigint NOT NULL DEFAULT 0;"
    )


def downgrade() -> None:
    op.execute(
        "ALTER TABLE public.billing_records DROP COLUMN IF EXISTS cached_tokens;"
    )
    op.execute(
        "ALTER TABLE public.usage_events DROP COLUMN IF EXISTS credits_charged;"
    )
