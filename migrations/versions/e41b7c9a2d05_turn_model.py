"""turn model

Revision ID: e41b7c9a2d05
Revises: d2f8a1c93e07
Create Date: 2026-09-22 23:05:00.000000

背景
----
会话地图的详情页要展示「每轮用的是哪个模型」与「缓存命中率」，而 ``turns`` 表此前
只写 token 用量：``cached_tokens`` / ``cache_hit`` 两列虽由 b8f4d2a91c37 加上了，
但**没有任何写入方**（引擎不回传、网关不写），因此恒为默认值、缓存命中率永远为 0。

本次增量：
* ``turns.model`` —— 本回合实际使用的模型名。
* 配套的写入方已就位：引擎把 ``model`` / ``cached_tokens`` 随 done 事件回传，
  网关在 ``FinishTurn`` 里落库（internal/session/manager.go）。

幂等：与既有迁移同一风格，重复执行不会破坏数据。
"""
from typing import Sequence, Union

from alembic import op

# revision identifiers, used by Alembic.
revision: str = 'e41b7c9a2d05'
down_revision: Union[str, Sequence[str], None] = 'd2f8a1c93e07'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema: 记录每轮实际使用的模型。"""
    op.execute("ALTER TABLE turns ADD COLUMN IF NOT EXISTS model varchar(128)")


def downgrade() -> None:
    """Downgrade schema: 移除每轮模型列。"""
    op.execute("ALTER TABLE turns DROP COLUMN IF EXISTS model")
