"""messages 增加 tool_call_id：让 `tool` 角色消息可配对

背景（已核实）
--------------
`messages` 表（`0001_authoritative_baseline.py:493-502`）只有：

    id / session_id / role / content / tool_calls / created_at / turn_id / source

**没有 `tool_call_id`**。而 OpenAI / Anthropic / DeepSeek 的消息契约要求：

    assistant.tool_calls = [{id: "call_1", ...}]
    tool:  {role: "tool", tool_call_id: "call_1", content: "..."}

没有这一列，从 `messages` 重建出的 `tool` 消息无法与发起它的 assistant 配对，
`_ensure_valid_tool_sequence`（`app/agent/compaction.py`）会把它们当**孤儿直接删除**。
于是"全角色入库"写进去的工具结果，读回来一条不剩 —— 等于没写。

为什么不让 tool 消息把 id 塞进 content
--------------------------------------
那会污染模型可见的正文（工具输出里凭空多出一行 id），并且依赖字符串解析来恢复
结构 —— 正是本仓库反复淘汰的"按措辞猜判"。

索引取舍
--------
不为 `tool_call_id` 单独建索引：它的查询形态是"按 session 取一页，然后在内存里
按 id 配对"（`order by seq` 已由 `ix_messages_session_seq` 覆盖）。单列索引在
这张表上只会增加写放大。

Revision ID: 0017_messages_tool_call_id
Revises: 0016_messages_seq
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0017_messages_tool_call_id"
down_revision: str | Sequence[str] | None = "0016_messages_seq"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # 可空：user/assistant 消息没有 tool_call_id；只有 role='tool' 的行会填。
    op.execute(
        "ALTER TABLE public.messages ADD COLUMN IF NOT EXISTS tool_call_id varchar(128)"
    )


def downgrade() -> None:
    op.execute("ALTER TABLE public.messages DROP COLUMN IF EXISTS tool_call_id")
