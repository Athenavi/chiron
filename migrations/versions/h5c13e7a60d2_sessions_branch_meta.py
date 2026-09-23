"""sessions 的分支元数据：branch_mode / branch_state / branch_keep_tail。

Revision ID: h5c13e7a60d2
Revises: g4b02d6e9f17
Create Date: 2026-09-23

为什么需要这三列（见 docs/session-map-branch-design.md）：

* `branch_mode`  —— `truncate`（旧行为：逐字复制前 N 条）或 `condense`
  （新增：把保留区的历史交给模型压成"核心上下文摘要"，只留最近 K 条原文）；
* `branch_state` —— `pending`（压缩任务排队中）/ `ready`（摘要已写入或无需压缩）
  / `failed`（压缩失败，会话仍可用）。没有它，前端只能看到一个"卡住的"新会话；
* `branch_keep_tail` —— 本次分支保留的原文条数，用于审计与复现（同一分叉点、
  不同 K 得到不同结果，事后要能解释）。

血缘本身（`parent_session_id` / `branch_from_seq`）已由既有迁移提供，这里不重复添加。
不加外键：删除父会话不应级联删除子分支（节点是引用，见会话地图的准则 2）。
"""

from typing import Sequence, Union

from alembic import op

revision: str = "h5c13e7a60d2"
down_revision: Union[str, Sequence[str], None] = "g4b02d6e9f17"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # 长度与枚举宽度对齐（truncate/condense、pending/ready/failed 都在 16 以内）
    op.execute("ALTER TABLE sessions ADD COLUMN IF NOT EXISTS branch_mode varchar(16)")
    op.execute("ALTER TABLE sessions ADD COLUMN IF NOT EXISTS branch_state varchar(16)")
    op.execute("ALTER TABLE sessions ADD COLUMN IF NOT EXISTS branch_keep_tail int")


def downgrade() -> None:
    op.execute("ALTER TABLE sessions DROP COLUMN IF EXISTS branch_keep_tail")
    op.execute("ALTER TABLE sessions DROP COLUMN IF EXISTS branch_state")
    op.execute("ALTER TABLE sessions DROP COLUMN IF EXISTS branch_mode")
