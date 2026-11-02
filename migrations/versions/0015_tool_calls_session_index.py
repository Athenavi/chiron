"""tool_calls 按会话查询的索引 + output 体积列

背景（已核实）
--------------
`tool_calls` 建表时只有 `(turn_id)` 一个索引（`migrations/sql/init.sql:625`），
而按会话读工具调用的查询是同一个形状：

    SELECT ... FROM tool_calls
     WHERE session_id = $1
     ORDER BY created_at DESC, id DESC
     LIMIT n

`session_id` 上没有索引 ⇒ **全表扫描 + 排序**。对比 `messages` 有
`ix_messages_session_created`，`tool_calls` 是唯一一张按会话查却没有会话索引的表。
会话越长、工具调用越多，翻页越慢。

为什么是复合索引而不是单列
--------------------------
查询的谓词与排序都需要：`session_id` 收窄，`created_at DESC, id DESC` 直接给序。
三列复合可以让 PG 保序扫描并提前终止。单列 `session_id` 只能收窄，排序仍要额外做。

为什么加 output_bytes
---------------------
`tool_calls.output` 是 `text`，写入侧**没有任何长度上限**
（`internal/api/tool_handler.go` 的 `fmt.Sprintf("%v", out)`）。一条工具结果可能
几百 KB 到几 MB，而列表查询过去会把整段 `output` 拉出来 —— 一页 50 条足以拖垮响应。

有了 `output_bytes`，列表可以只取长度、不取正文，点开详情时再按需分段读。
**存量行必须回填**，否则历史记录在界面上会显示为"0 字节"，而实际有内容。

Revision ID: 0015_tool_calls_session_index
Revises: 0014_llm_models_max_output_tokens
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0015_tool_calls_session_index"
down_revision: str | Sequence[str] | None = "0014_llm_models_max_output_tokens"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # IF NOT EXISTS：可重入，重复执行不报错（与 0013 / 0014 同一取向）。
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_tool_calls_session_created "
        "ON public.tool_calls USING btree (session_id, created_at DESC, id DESC)"
    )
    op.execute("ALTER TABLE public.tool_calls ADD COLUMN IF NOT EXISTS output_bytes integer")
    # 回填存量行：只在仍为 NULL 时写，重复执行无副作用。
    # octet_length 而非 length：前者按字节计，与"这条结果下载下来多大"一致。
    op.execute(
        "UPDATE public.tool_calls "
        "SET output_bytes = octet_length(output) "
        "WHERE output_bytes IS NULL"
    )


def downgrade() -> None:
    op.execute("ALTER TABLE public.tool_calls DROP COLUMN IF EXISTS output_bytes")
    op.execute("DROP INDEX IF EXISTS public.ix_tool_calls_session_created")
