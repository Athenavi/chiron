"""thread_deliveries.payload：投递正文快照（P1-1 接线先决）

`enqueueThreadDelivery` 当初只存键不存正文，前提是"投递时能按 kind 重新拼出正文"。
但那前提不成立：引擎回投是**一次性 HTTP 请求**，payload（`status` / `profile` /
`summary`）不在任何表里留底，drain 侧拿不到它们，`buildSubagentFollowupContent` /
`buildJobFollowupContent` 就拼不出正文。

所以正文的**输入**必须随投递行一起落库。选 `jsonb` 单列而不是 `status/profile/summary`
三列：这三项随 kind 变化（`subagent` 要 `profile`，`job` 不要），拆成列会得到一张大部分
格子为空的稀疏表；而 jsonb 存的是"事件到达那一刻的输入快照"，本身就该是不可变事实，
不需要被 SQL 按字段查询。

纯扩展：`ADD COLUMN` 带默认值，旧代码不写这一列也照常工作（扩展-迁移-收缩的扩展阶段）。

Revision ID: 0022_thread_delivery_payload
Revises: 0021_threads
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0022_thread_delivery_payload"
down_revision: str | Sequence[str] | None = "0021_threads"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # 默认空对象而不是 NULL：投递行的 payload 是"这次要投什么"的输入，
    # 空对象与 NULL 在应用侧都要单独判空，不如统一成一种"没有额外输入"的表达。
    op.execute(
        "ALTER TABLE public.thread_deliveries "
        "ADD COLUMN IF NOT EXISTS payload jsonb NOT NULL DEFAULT '{}'::jsonb"
    )


def downgrade() -> None:
    op.execute("ALTER TABLE public.thread_deliveries DROP COLUMN IF EXISTS payload")
