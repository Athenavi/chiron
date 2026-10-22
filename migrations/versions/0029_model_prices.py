"""model_prices：按模型的公开价格（M28）

Revision ID: 0029_model_prices
Revises: 0028_key_weights
Create Date: 2026-10-22

背景（M28）：本仓的单价是**全局**的（`BillingConfig.LLMCostPerToken/Output`，1 / 2 credits
per 1000 tokens），对所有模型一视同仁 —— 而不同模型的价格差几个数量级。这张表存**按模型**的
公开价格（由定时任务从多个公开数据源抓取 + 交叉验证后写入），供成本核算/成本轴单价/按成本选
模型使用。

⚠ **它不参与计费**：计费的权威是 `BillingConfig`（管理员可改，`billing_records` 按它记账）。
这张表是**参考数据**，混进计费会让"谁说了算"变得不可追溯。

⚠ 主键是 `(model, provider)`：同一个模型经不同渠道（如 OpenRouter / 直连）价格不同，
`provider` 为空串表示"通用/未指明渠道"。

`source_count` 与 `sources` 是**交叉验证的痕迹**：多源比对通过才写入，`source_count >= 2`
说明这个值至少被两个源确认过；只有单源时按"未验证"处理（不写或标 `source_count = 1`）。
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0029_model_prices"
down_revision: str | Sequence[str] | None = "0028_key_weights"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS model_prices (
            model          varchar(128) NOT NULL,
            provider       varchar(50)  NOT NULL DEFAULT '',
            input_per_1k   numeric(12,6) NOT NULL DEFAULT 0,
            output_per_1k  numeric(12,6) NOT NULL DEFAULT 0,
            currency       varchar(8)   NOT NULL DEFAULT 'USD',
            sources        text         NOT NULL DEFAULT '',
            source_count   integer      NOT NULL DEFAULT 0,
            verified_at    timestamptz  NOT NULL DEFAULT now(),
            updated_at     timestamptz  NOT NULL DEFAULT now(),
            PRIMARY KEY (model, provider)
        )
        """
    )
    # 读路径是"按模型名查价"（成本核算）与"看最近一次同步写了多少行"（观测）。
    op.execute("CREATE INDEX IF NOT EXISTS ix_model_prices_verified_at ON model_prices (verified_at)")


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS ix_model_prices_verified_at")
    op.execute("DROP TABLE IF EXISTS model_prices")
