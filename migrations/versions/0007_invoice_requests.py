"""发票申请表 invoice_requests —— 工单式开票（用户申请 / 管理员审核）。

背景（已核实）
--------------
`payments(id, user_id, channel, credits, amount_cents, currency, status, qr_code,
provider_order_id, trade_no, created_at, paid_at, expired_at)` 早已存在，
`status ∈ pending / paid / failed`（`internal/billing/payments.go`），有
`idx_payments_user`。但全仓**没有任何开票实现** —— 用户想报销只能找人工。

做法取舍
--------
1. **工单式，不接第三方开票平台**：接平台涉及资质与对账，是产品/合规问题而非
   工程问题。一期只做"申请 → 审核 → 线下开票"的流转，把状态与金额留在系统里可查。
2. **金额与币种做快照**，不靠 JOIN `payments` 现读：已开票的金额必须固定。
   若将来支付记录被修正（退款、对账调整），历史发票上的数字不该跟着变。
3. **`UNIQUE (payment_id)`**：一笔支付只能开一次票，这是本期"重复申请拦截"的
   唯一真相源。判定靠唯一索引（`23505`），不做"先查后插" —— 后者在并发下
   两个请求都能查到"没有"，然后都插进去。
4. **不建外键到 `payments`**：写入走
   `INSERT ... SELECT ... FROM payments WHERE id = $1 AND user_id = $2 AND status = 'paid'`，
   校验与写入在同一条语句里完成。加外键并不能多拦住什么（存在性已由该 SELECT
   保证），却会让"清理历史支付数据"这类运维动作被一张申请表挡住。

状态机
------
`pending → approved | rejected`，**单向**。约束放在两个地方：

  - 数据库层 `CHECK (status IN (...))` 拦非法取值；
  - 更新语句里带 `AND status = 'pending'`，重复审核影响 0 行（并发双审时
    只有第一个生效，第二个不会被静默覆盖）。

一期口径（roadmap §11.4）：只支持**全额开票 + 不可撤销**。部分开票需要
"一张支付拆多条发票 + 剩余可开金额"这套账，退款后已开票的处理也需要产品定义；
没有真实诉求前不实现。

字段说明
--------
按本文档的字段清单建表，只多一个
`currency`（快照所需）与 `review_note`（审核意见，与用户可见状态一起展示）。
没有加"收票邮箱/电话"：一期由管理员线下联系，加了字段也没人读。

Revision ID: 0007_invoice_requests
Revises: 0006_api_keys_key_hash_index
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0007_invoice_requests"
down_revision: str | Sequence[str] | None = "0006_api_keys_key_hash_index"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS public.invoice_requests
        (
            id            varchar(36) PRIMARY KEY,
            user_id       varchar(36) NOT NULL,
            -- 宽度必须与 payments.id 的 varchar(64) 一致：不同宽度会让等值比较
            -- 走隐式转换，索引失效。
            payment_id    varchar(64) NOT NULL,
            title         varchar(255) NOT NULL,
            tax_no        varchar(64)  NOT NULL,
            -- 快照：申请时的金额，之后不随 payments 变化。
            amount_cents  bigint      NOT NULL,
            currency      varchar(8)  NOT NULL DEFAULT 'CNY',
            status        varchar(16) NOT NULL DEFAULT 'pending',
            note          text,
            review_note   text,
            reviewed_by   varchar(36),
            reviewed_at   timestamptz,
            created_at    timestamptz NOT NULL DEFAULT now(),
            CONSTRAINT invoice_requests_status_check
                CHECK (status IN ('pending', 'approved', 'rejected'))
        )
        """
    )
    # 重复申请拦截的唯一真相源：一笔支付只能有一张发票。
    # 用唯一约束而不是普通索引，是因为判定依赖 23505。
    op.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS ux_invoice_requests_payment "
        "ON public.invoice_requests USING btree (payment_id)"
    )
    # 用户查"我申请过哪些票"、管理端按时间翻待审队列都走这条。
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_invoice_requests_user_time "
        "ON public.invoice_requests USING btree (user_id, created_at DESC)"
    )
    # 管理端待审列表：`WHERE status = 'pending' ORDER BY created_at`
    # 只扫待审行，已处理的历史不会拖慢队列。
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_invoice_requests_pending "
        "ON public.invoice_requests USING btree (created_at DESC) "
        "WHERE (status = 'pending')"
    )


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS public.invoice_requests")
