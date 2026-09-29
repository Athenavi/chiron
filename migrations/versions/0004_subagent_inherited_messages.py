"""subagent_runs 增审计列 inherited_messages（方案 01 §4.2 · S2）

Revision ID: 0004_subagent_inherited_messages
Revises: 0003_agent_runs
Create Date: 2026-09-30

S2（fork 子 agent）的验收要求「审计：`subagent_runs` 增 `inherited_messages` 计数，
前端可见"这个子 agent 看到了父的多少上下文"」（方案 01 §4.2、评审 01 §1.9）。

**为什么单独一条迁移、且引擎侧用单独 UPDATE 写**：与 `rerun_of` 同一模式
（见 `app/subagent/store.py` 的 `RERUN_OF_SQL` 注释）。若把新列并进 `RUN_INSERT_SQL`，
未执行本迁移的库在**普通**子 agent 派发时整条 INSERT 都会因未知列失败 ——
那是把"审计字段缺失"升级成"子 agent 落库全挂"。单独 UPDATE 的最坏后果只是这个计数为 NULL。

**列可空（无默认值）**：NULL 精确表达"没有这个信息"（未迁移 / 未开启继承），
不必伪装成"继承了 0 条"—— 与 A5 用 NULL 表达"不含明细"同一理由（D12）。

``IF NOT EXISTS``：与既有迁移风格一致（某些环境若已手工加列则不覆盖）。
"""

from typing import Sequence, Union

from alembic import op

revision: str = "0004_subagent_inherited_messages"
down_revision: Union[str, Sequence[str], None] = "0003_agent_runs"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute(
        """
        ALTER TABLE subagent_runs
            ADD COLUMN IF NOT EXISTS inherited_messages integer
        """
    )


def downgrade() -> None:
    op.execute("ALTER TABLE subagent_runs DROP COLUMN IF EXISTS inherited_messages")
