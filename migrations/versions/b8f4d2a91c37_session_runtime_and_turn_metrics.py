"""session runtime state and turn cache metrics

Revision ID: b8f4d2a91c37
Revises: 5e244b718fd1
Create Date: 2026-09-21 20:50:00.000000

P1（docs/session-runtime-spec.md）所需的两处 schema 变更：

* ``unified_sessions.runtime`` —— 会话运行时状态的**权威持久层**
  （模式 / 模型 / provider / 工具授权 / 上下文激活项；结构见 spec §2.1，
  ``context`` 与既有 ``workbench_context`` 同构，零映射）。
  Redis 侧的 ``session:runtime:{tenant}:{sid}`` 只是热缓存，缺失时由本列回填。
* ``turns.cached_tokens`` / ``turns.cache_hit`` —— 会话遥测的持久层补充
  （供应商侧 prompt cache 命中量、语义缓存是否命中）。
  其余遥测（费用、tokens）沿用既有 ``billing_records`` / ``turns.input|output_tokens``，
  不新建表：实时层在 Redis（``session:metrics:{tenant}:{sid}``，读取时聚合）。
"""
from typing import Sequence, Union

from alembic import op


# revision identifiers, used by Alembic.
revision: str = 'b8f4d2a91c37'
down_revision: Union[str, Sequence[str], None] = '5e244b718fd1'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema: 会话运行时状态列 + 轮次缓存遥测列。"""
    op.execute("ALTER TABLE unified_sessions ADD COLUMN IF NOT EXISTS runtime jsonb NOT NULL DEFAULT '{}'::jsonb")
    op.execute("ALTER TABLE turns ADD COLUMN IF NOT EXISTS cached_tokens bigint NOT NULL DEFAULT 0")
    op.execute("ALTER TABLE turns ADD COLUMN IF NOT EXISTS cache_hit boolean NOT NULL DEFAULT false")
    # 会话时间序查询（统计端点按 created_at 倒序取最近 N 轮）
    op.execute("CREATE INDEX IF NOT EXISTS turns_session_created_idx ON turns (session_id, created_at)")


def downgrade() -> None:
    """Downgrade schema: 移除上述列（数据不可恢复，仅结构回滚）。"""
    op.execute("DROP INDEX IF EXISTS turns_session_created_idx")
    op.execute("ALTER TABLE turns DROP COLUMN IF EXISTS cache_hit")
    op.execute("ALTER TABLE turns DROP COLUMN IF EXISTS cached_tokens")
    op.execute("ALTER TABLE unified_sessions DROP COLUMN IF EXISTS runtime")
