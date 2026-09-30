"""subagent_runs 增生命周期遥测列（方案 04 §3 · A5）

Revision ID: 0006_subagent_lifecycle
Revises: 0005_memory_project
Create Date: 2026-10-06

A5 的目标是让"子 Agent 出问题时"**可诊断**，而对齐的参照（Reasonix 的 `SubagentLifecycleInfo`）
把这类信息称为 **content-free 宿主遥测**：只记"发生了什么"，**刻意不含** prompt / 推理 / 工具输出 /
路径。本迁移只加列，不加任何内容字段。

五列及语义：

* ``retryable`` —— 这次失败**是否值得重试**。与 ``error`` 分开的理由：错误文本是给人读的，
  重试与否是给调度器判的，把判断塞进字符串解析是脆的。
* ``output_bytes`` —— 产出体量。用**字节数**而不是全文：既能算"这个子 Agent 产了多少"，
  又不把内容搬进审计链路。
* ``validator_mode`` / ``validator_outcome`` / ``validator_attempt`` —— 收尾校验的
  **模式**（如是否强制结构化）、**结论**、**第几次尝试**。这三列解决的是同一个问题：
  现在只能从 ``summary`` 的形态猜"校验过没有、过了几次"。

为什么单独迁移而不是并进别处：与 ``rerun_of``（0003 后的 f3a91c2d5e08）和
``inherited_messages``（0004）同一理由 —— 让"未迁移的部署"只丢遥测，**不影响子 Agent 落库本身**。
对应地，``app/subagent/store.py`` 里的写入也是**单独 UPDATE**（见该文件的 ``LIFECYCLE_SQL``）。

既有行取默认值即语义正确：``retryable=false`` / ``output_bytes=0`` / 校验三列为空或 0
= "当时没有这个信息"，不需要回填。

``IF NOT EXISTS`` 与既有迁移风格一致（某些环境可能已手工改过）。
"""

from typing import Sequence, Union

from alembic import op

revision: str = "0006_subagent_lifecycle"
down_revision: Union[str, Sequence[str], None] = "0005_memory_project"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute(
        "ALTER TABLE subagent_runs "
        "ADD COLUMN IF NOT EXISTS retryable boolean NOT NULL DEFAULT false"
    )
    op.execute(
        "ALTER TABLE subagent_runs "
        "ADD COLUMN IF NOT EXISTS output_bytes bigint NOT NULL DEFAULT 0"
    )
    op.execute(
        "ALTER TABLE subagent_runs "
        "ADD COLUMN IF NOT EXISTS validator_mode character varying(16)"
    )
    op.execute(
        "ALTER TABLE subagent_runs "
        "ADD COLUMN IF NOT EXISTS validator_outcome character varying(16)"
    )
    op.execute(
        "ALTER TABLE subagent_runs "
        "ADD COLUMN IF NOT EXISTS validator_attempt integer NOT NULL DEFAULT 0"
    )


def downgrade() -> None:
    for column in (
        "validator_attempt",
        "validator_outcome",
        "validator_mode",
        "output_bytes",
        "retryable",
    ):
        op.execute(f"ALTER TABLE subagent_runs DROP COLUMN IF EXISTS {column}")
