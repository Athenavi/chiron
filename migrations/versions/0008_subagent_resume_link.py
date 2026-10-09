"""subagent_runs 增「继续 / 分叉」血缘列（R5(b)，设计见 docs/subagent-resume-design.md）

Revision ID: 0008_subagent_resume_link
Revises: 0007_subagent_error_code
Create Date: 2026-10-09

R5(b)：从一个**已存在**的子 agent run「继续」跑，或从它的某一步「分叉」。缺口只有一件
（`rerun_subagent` 与 `rerun_of` 早已落地，它们做的是"**按原任务再派一次**、不带步骤"）：
**带着已记录的步骤继续**。

两列：

* ``resumed_from`` —— 被续跑 / 被分叉的那个 run id（血缘，与 ``rerun_of`` 并列但不同源）；
* ``resume_at_step`` —— 分叉点：只继承 ``seq < resume_at_step`` 的步骤；``NULL`` = **续到底**。

**为什么是加列而不是新建 `subagent_run_links` 表**：一个列就能表达 1:N
（``WHERE resumed_from = $1``），而新表要多一条 join 与一套生命周期；当前只有两种关系
（``rerun_of`` 也已经在列上）。见设计 §1 的取舍。

**为什么单独一条迁移、且引擎侧用单独 UPDATE 写**：与 ``rerun_of``（f3a91c2d5e08）和
``inherited_messages``（0004）同一模式 —— 若把新列并进 ``RUN_INSERT_SQL``，未执行本迁移的库在
**普通**子 agent 派发时整条 INSERT 都会因未知列失败（把"血缘缺失"升级成"子 agent 落库全挂"）。
单独 UPDATE 的最坏后果只是这两列为 NULL。

**两列都可空（无默认值）**：NULL 精确表达"没有这个信息"（未迁移 / 不是续跑），
不必伪装成"从空 run 续来" —— 与 D12（A5 用 NULL 表达"不含明细"）同一理由。

``IF NOT EXISTS``：与既有迁移风格一致。
"""

from typing import Sequence, Union

from alembic import op

revision: str = "0008_subagent_resume_link"
down_revision: Union[str, Sequence[str], None] = "0007_subagent_error_code"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute(
        """
        ALTER TABLE subagent_runs
            ADD COLUMN IF NOT EXISTS resumed_from character varying(64)
        """
    )
    op.execute(
        """
        ALTER TABLE subagent_runs
            ADD COLUMN IF NOT EXISTS resume_at_step integer
        """
    )


def downgrade() -> None:
    op.execute("ALTER TABLE subagent_runs DROP COLUMN IF EXISTS resume_at_step")
    op.execute("ALTER TABLE subagent_runs DROP COLUMN IF EXISTS resumed_from")
