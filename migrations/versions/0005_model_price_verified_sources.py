"""model_prices 增加"可信度"与每源明细。

**为什么需要**：原同步逻辑只把「≥2 个源在 5% 容差内一致」的模型写进表（实测 38 行），
单源模型与源间有分歧的模型一律丢弃。结果是价格页能看到的模型远少于实际市场 ——
用户看不到「这个模型只有一家报价」或「两家报价差多少」，也就无从判断。

本次给每行补两个字段：
- ``verified``：是否通过多源印证（≥2 源在容差内一致）。单源/分歧的行也会写入，
  但标记为 false —— 显示归显示，不假装它被验证过。
- ``source_details``：``[{"source": "...", "input_per_1k": 0.0, "output_per_1k": 0.0}]``，
  供页面在「印证来源」一栏展开查看每个源各自的报价。

存量行按 ``source_count >= 2`` 回填（旧逻辑只有这种情况下才会写入）。

Revision ID: 0005_model_price_verified_sources
Revises: 0004_audit_log_created_at
"""

from typing import Sequence, Union

from alembic import op

revision: str = "0005_model_price_verified_sources"
down_revision: Union[str, Sequence[str], None] = "0004_audit_log_created_at"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute(
        "ALTER TABLE public.model_prices "
        "ADD COLUMN IF NOT EXISTS verified boolean NOT NULL DEFAULT false"
    )
    op.execute(
        "ALTER TABLE public.model_prices "
        "ADD COLUMN IF NOT EXISTS source_details jsonb NOT NULL DEFAULT '[]'::jsonb"
    )
    # 旧同步只写"已验证"的行，按 source_count 回填即可还原当时的语义。
    op.execute(
        "UPDATE public.model_prices SET verified = (source_count >= 2) WHERE verified = false"
    )


def downgrade() -> None:
    op.execute("ALTER TABLE public.model_prices DROP COLUMN IF EXISTS source_details")
    op.execute("ALTER TABLE public.model_prices DROP COLUMN IF EXISTS verified")
