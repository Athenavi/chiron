"""llm_provider_keys 增每 key 权重（M27）

Revision ID: 0028_key_weights
Revises: 0027_drop_python_settings
Create Date: 2026-10-22

背景（M27）：KeyRing 的选择一直是**纯 LRU 轮转**（Redis 单键 EVAL 选 `u` 最小的那把），
没有"某把 key 该多分流量"的表达能力 —— 而真实部署里确实存在这种需求（不同账号的额度 /
折扣不同、新老 key 要错峰下线）。

`weight` 默认 **1**（每把等权 = 现在的行为），`0` 表示**不参与选择**（把流量从这把 key 上
摘掉，但保留它的状态与备注 —— 比改 `status` 更适合灰度下线）。权重只在**可用**的 key 之间
分配：冷却中的 key 仍然直接不参与（与冷却正交）。

⚠ 不加 `CHECK (weight >= 0)`：与既有列的风格一致（本仓的约束多写在代码里），且负权重在
Lua 侧一律按"不参与"处理。
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0028_key_weights"
down_revision: str | Sequence[str] | None = "0027_drop_python_settings"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        "ALTER TABLE llm_provider_keys ADD COLUMN IF NOT EXISTS weight integer NOT NULL DEFAULT 1"
    )


def downgrade() -> None:
    op.execute("ALTER TABLE llm_provider_keys DROP COLUMN IF EXISTS weight")
