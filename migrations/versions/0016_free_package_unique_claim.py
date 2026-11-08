"""0 元套餐（试用包）防重复领取：payments 上的部分唯一索引。

背景：`claimFreePackage` 是「先查 user_entitlements 再插 payments」，
并发点击（或前端重试）时两个请求都能查到"没领过"，于是各落一笔订单、
各发一份权益 —— 免费包没有金额可以兜底，重复发放是纯损失。

为什么约束加在 `payments` 而不是 `user_entitlements`：
- `user_entitlements` 已有 `ux_user_entitlements_payment`（按 payment_id 唯一），
  它只能保证"一笔订单不会发两次权益"，管不住"同一个包被领两次"；
- 订单才是申请的载体，在落单这一刻拦住重复，比等权益发放时才发现更早。

为什么是**部分**索引（`WHERE channel = 'free'`）：付费套餐本来就允许重复购买
（续费/叠加），只有 0 元包必须一生一次。

Revision ID: 0016_free_package_unique_claim
Revises: 0015_tenant_quota_requests
"""

from typing import Sequence, Union

from alembic import op

revision: str = "0016_free_package_unique_claim"
down_revision: Union[str, Sequence[str], None] = "0015_tenant_quota_requests"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # package_code 可空：免费包一定带 code，但既有纯充值订单没有。
    # 用 COALESCE 把 NULL 归一，避免 NULL 之间互不相等导致索引形同虚设。
    op.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS ux_payments_free_package_per_user "
        "ON public.payments (user_id, COALESCE(package_code, '')) "
        "WHERE channel = 'free';"
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS public.ux_payments_free_package_per_user;")
