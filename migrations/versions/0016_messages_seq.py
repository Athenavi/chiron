"""messages 增加单调序号 seq

背景（已核实）
--------------
`messages` 表（`migrations/sql/init.sql:923`）以 `id varchar(36)` 为主键，
排序一律用 `ORDER BY created_at DESC, id DESC`。而 `created_at` 是
**`timestamp`（无时区）**，且由应用侧 `NOW()` 写入：

- 多副本同时写同一会话时，`NOW()` 可能落在同一毫秒；
- `timestamp` 不带时区，跨机房/跨副本的时钟偏差会直接体现为顺序错乱；
- 作为游标时，`(created_at, id)` 在 `created_at` 相等的情况下只能靠随机 UUID 兜底，
  等于"同一毫秒内的消息顺序不稳定"。

后果不是"看起来乱"，而是**分页会漏消息**：游标 `(created_at, id) < (t, id0)`
在并发追加下，同一毫秒内 id 排序靠前的消息可能被永久跳过。

为什么用 IDENTITY 而不是触发器/应用侧赋值
-----------------------------------------
`GENERATED ALWAYS AS IDENTITY` 由序列在**插入时**分配，单调递增、无并发争用、
不需要应用配合。应用侧赋值会在多副本下重新引入竞争（要靠 `SELECT nextval` 或
`RETURNING`，而写入方不止一处）。

为什么同时建 `(session_id, seq)` 唯一索引
-----------------------------------------
`seq` 是**全表**单调的，会话内查询需要的是"按会话取最近 N 条"。复合唯一索引
同时满足两点：给 `WHERE session_id = $1 ORDER BY seq DESC` 提供保序扫描，
并把"同一会话内序号重复"变成数据库级不可能。

**顺序语义的唯一正确用法**：排序一律 `ORDER BY seq`。不要把 `seq` 与
`created_at` 混在同一排序里比较 —— 存量行的 `seq` 按物理顺序回填，与
`created_at` 不保证一致（见下）。

回填说明
--------
在已有数据的表上添加 IDENTITY 列，PostgreSQL 会**重写整表**并锁写。本表是会话
消息表，行数随使用增长；发布窗口内需要评估耗时，或改为「先加可空列、后台回填、
再加约束」的三段式。当前迁移采用一次性重写，前提是本仓库的迁移都随发布窗口执行
（与 0013 的取向一致）。

存量行的 `seq` 按物理顺序分配，与 `created_at` 的相对顺序**可能不一致**。这不影响
正确性（新数据严格单调），但意味着不能拿旧数据校验"seq 与时间同序"。

Revision ID: 0016_messages_seq
Revises: 0015_tool_calls_session_index
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0016_messages_seq"
down_revision: str | Sequence[str] | None = "0015_tool_calls_session_index"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        "ALTER TABLE public.messages "
        "ADD COLUMN IF NOT EXISTS seq bigint GENERATED ALWAYS AS IDENTITY"
    )
    op.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS ix_messages_session_seq "
        "ON public.messages (session_id, seq)"
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS public.ix_messages_session_seq")
    op.execute("ALTER TABLE public.messages DROP COLUMN IF EXISTS seq")
