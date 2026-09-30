"""subagent_runs 增结构化的错误码（R3，方案见 vendor/规划.md §3.5）

Revision ID: 0007_subagent_error_code
Revises: 0006_subagent_lifecycle
Create Date: 2026-10-06

R3 把"这次失败能不能重试"从**拼接字符串**里解出来，变成机器可读的结论。`retryable` 列（0006）已经
在了，这里补它的搭档：**错误码**。

为什么需要单独一列、而不是从 `error` 文本里猜：

* `error` 是给人读的**自由文本**（`" | ".join(errors)`），形态会随实现漂移；
* 调用方要的是**能分支的判断**（重试 / 换模型 / 报给用户），它需要一个**有限枚举**。
  自由文本只能被"读"，不能被"判断"。

取值与"能否重试"的对应关系在 `app/subagent/outcome.py`（`ERROR_CODES` / `RETRYABLE_CODES`），
**不在**本迁移里 —— 迁移只加列，语义留在代码里一处。

可空且**不回填**：既有行没有这个信息，写 `NULL` 比写 `'unknown'` 诚实（"当时没记"≠"当时判不出"）。

``IF NOT EXISTS`` 与既有迁移风格一致。
"""

from typing import Sequence, Union

from alembic import op

revision: str = "0007_subagent_error_code"
down_revision: Union[str, Sequence[str], None] = "0006_subagent_lifecycle"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute(
        "ALTER TABLE subagent_runs "
        "ADD COLUMN IF NOT EXISTS error_code character varying(32)"
    )


def downgrade() -> None:
    op.execute("ALTER TABLE subagent_runs DROP COLUMN IF EXISTS error_code")
