"""支付退款记录（租户级）。

退款是独立的记录：一个订单可以多次部分退款，每次一条；金额与状态写入后不再变更
（失败重试会更新原记录状态，不新增重复行）。租户通过 `payments.user_id → users.tenant_id`
归属，不新增支付表字段。

Revision ID: 0012_payment_refunds
Revises: 0011_package_templates
"""

from typing import Sequence, Union

from alembic import op

revision: str = "0012_payment_refunds"
down_revision: Union[str, Sequence[str], None] = "0011_package_templates"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS public.payment_refunds (
            id character varying(64) NOT NULL PRIMARY KEY,
            payment_id character varying(64) NOT NULL,
            tenant_id smallint NOT NULL DEFAULT 0,
            user_id character varying(36),
            channel character varying(16) NOT NULL,
            amount_cents bigint NOT NULL,
            currency character varying(8) NOT NULL DEFAULT 'CNY',
            reason text NOT NULL DEFAULT '',
            status character varying(16) NOT NULL DEFAULT 'pending',
            operator_id character varying(36),
            channel_refund_id character varying(128) NOT NULL DEFAULT '',
            error_message text NOT NULL DEFAULT '',
            created_at timestamp without time zone NOT NULL DEFAULT now(),
            updated_at timestamp without time zone NOT NULL DEFAULT now()
        );
        """
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_payment_refunds_payment "
        "ON public.payment_refunds (payment_id);"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_payment_refunds_tenant_status "
        "ON public.payment_refunds (tenant_id, status, created_at DESC);"
    )


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS public.payment_refunds;")
