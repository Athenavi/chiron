"""sessions.alias —— 会话别名/备注（地图里"备注别名"的持久层）。

Revision ID: g4b02d6e9f17
Revises: f3a91c2d5e08
Create Date: 2026-09-23

为什么要单独一列而不是复用 `title`：
* `title` 是**会话本身的名字**（模型/系统也会写它，例如首条消息摘要、fork 的标题）；
* `alias` 是**用户给这个会话起的别名/备注**，只由用户写入，且**展示时优先于 title**
  （`displayName = alias || title`）。把两者混在一列里，任何一次自动改标题都会抹掉用户的备注。

长度与 `tag` 对齐（varchar(64)）：它同样是"一眼可读的标注"，不是正文。
"""

from typing import Sequence, Union

from alembic import op

revision: str = "g4b02d6e9f17"
down_revision: Union[str, Sequence[str], None] = "f3a91c2d5e08"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute("ALTER TABLE sessions ADD COLUMN IF NOT EXISTS alias varchar(64)")


def downgrade() -> None:
    op.execute("ALTER TABLE sessions DROP COLUMN IF EXISTS alias")
