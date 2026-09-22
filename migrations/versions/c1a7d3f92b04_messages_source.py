"""messages.source — 标记消息来源（子 Agent 自动轮）

Revision ID: c1a7d3f92b04
Revises: b8f4d2a91c37
Create Date: 2026-09-22

背景
----
子 Agent 的「自动唤起父会话新一轮」（``internal/api/agent_followup.go``）需要把一条
**机器生成**的消息写进对话历史：它复用 ``messages`` 表与 ``role='user'``（这样主 Agent
与既有的历史拼装逻辑都不用特判），但**不能**在界面上伪装成用户提问。

因此新增 ``source`` 列作为权威标记，前端据此区分渲染（系统卡片 vs 用户气泡）：

* ``''``（默认）—— 用户正常输入；
* ``subagent_followup`` —— 子 Agent 完成后的自动轮注入。

幂等性
------
``ADD COLUMN IF NOT EXISTS``：列已存在时重复执行不会报错，与既有库
（``migrations/sql/init.sql`` 建表 + ``alembic stamp`` 对齐）的部署方式兼容。
"""
from typing import Sequence, Union

from alembic import op

# revision identifiers, used by Alembic.
revision: str = 'c1a7d3f92b04'
down_revision: Union[str, Sequence[str], None] = 'b8f4d2a91c37'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute(
        "ALTER TABLE messages ADD COLUMN IF NOT EXISTS source VARCHAR(32) NOT NULL DEFAULT ''"
    )
    op.execute(
        "COMMENT ON COLUMN messages.source IS "
        "'消息来源：空=用户输入；subagent_followup=子 Agent 自动轮注入'"
    )


def downgrade() -> None:
    op.execute("ALTER TABLE messages DROP COLUMN IF EXISTS source")
