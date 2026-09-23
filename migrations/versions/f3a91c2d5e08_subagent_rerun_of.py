"""subagent_runs.rerun_of —— 重跑血缘（P4 后续能力：中断后可重跑）。

Revision ID: f3a91c2d5e08
Revises: e41b7c9a2d05
Create Date: 2026-09-23

为什么要一列而不是"记在 Redis 里"：血缘是**审计与重跑链**的一部分 ——
"这个 run 是从哪一次重跑来的"必须在 DB 里可查（Redis 会过期、会丢）。
重跑入口见 python-engine/app/tools/subagent_rerun.py。
"""

from typing import Sequence, Union

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "f3a91c2d5e08"
down_revision: Union[str, Sequence[str], None] = "e41b7c9a2d05"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """加一列：本次 run 由哪个 run 重跑而来（NULL = 首次派发）。"""
    op.execute(
        "ALTER TABLE subagent_runs ADD COLUMN IF NOT EXISTS rerun_of varchar(64)"
    )


def downgrade() -> None:
    op.execute("ALTER TABLE subagent_runs DROP COLUMN IF EXISTS rerun_of")
