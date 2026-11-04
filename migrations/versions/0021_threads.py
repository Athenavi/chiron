"""threads：事件来源、订阅与投递事实（P1-1）

数据模型与 §6 问题 3 的裁决绑定：**串行排队**。Thread 只承担"这条事件从哪来、
要投给谁"，不承担执行职责 —— 执行侧仍是 `agent:run-lock:<session_id>` 一把锁。
所以本文件里**没有** `session.parent_id`，也没有任何"并行"字段：加上去就等于
在数据层承认了子会话，而那条路要走的是独立架构变更（见 roadmap P1-1.1）。

三张表都是**纯扩展**：不 ALTER 任何既有表，不动 `messages`。

`threads`
---------
一条 thread = 一个事件来源的上下文。`external_key` 是上游的幂等键。
`threads_idem` 这条部分唯一索引是去重的主力：多副本 + 队列重投会让同一条事件
重复入站，靠应用层"先查后插"挡不住。空串排除在外，因为手建/内部触发的 thread
没有幂等键，不该因为它们只能存在一条。

`source_kind` 的取值集合跟 P0-2 的 `followupKinds` 走（`subagent` / `job`），
**不含 `webhook`** —— 外部自注册订阅会新增信任边界，属独立批次，不在这里开口子。

`thread_subscriptions`
----------------------
事件按它直接找到目标会话，绕开协调者。它是**用户配置**（丢了要人重填），
所以不是事实源，不参与"订阅丢失后能否从事实重建"的判定。

`thread_deliveries`
-------------------
这才是那半个答案：投递是**事实**，必须可查、可续、可去重。
`primary key (thread_id, event_key)` 让"同一事件投第二次"在数据库层直接冲突，
不需要额外的幂等表或分布式锁 —— 与 `ux_superagent_active` 同一思路。

`session_id` 故意**不加外键**到 `sessions`：删会话不该连带抹掉投递历史
（那是审计证据）。悬空行由 `status` 表达（会话没了，投递留着，状态停在终态）。

Revision ID: 0021_threads
Revises: 0020_superagent
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0021_threads"
down_revision: str | Sequence[str] | None = "0020_superagent"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # tenant_id / user_id 的类型跟着既有表走（sessions.tenant_id 是 smallint），
    # 自造一个 bigint 会在这里就建不出来。
    op.execute(
        "CREATE TABLE IF NOT EXISTS public.threads "
        "( "
        "    id           varchar(36)  NOT NULL PRIMARY KEY, "
        "    tenant_id    smallint     NOT NULL REFERENCES public.tenants, "
        "    user_id      varchar(36)  NOT NULL REFERENCES public.users, "
        "    source_kind  varchar(32)  NOT NULL, "
        "    external_key varchar(128) NOT NULL DEFAULT '', "
        "    created_at   timestamptz  NOT NULL DEFAULT now() "
        ")"
    )
    op.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS threads_idem "
        "ON public.threads (tenant_id, source_kind, external_key) "
        "WHERE external_key <> ''"
    )
    # 归属查询（"这个用户有哪些 thread"）按 (user_id, tenant_id) 收窄，
    # 与 superagent 那两张表的索引形态一致。
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_threads_owner "
        "ON public.threads (user_id, tenant_id)"
    )

    op.execute(
        "CREATE TABLE IF NOT EXISTS public.thread_subscriptions "
        "( "
        "    id         varchar(36) NOT NULL PRIMARY KEY, "
        "    thread_id  varchar(36) NOT NULL REFERENCES public.threads (id) ON DELETE CASCADE, "
        "    session_id varchar(36) NOT NULL, "
        "    status     varchar(16) NOT NULL DEFAULT 'active', "
        "    created_at timestamptz NOT NULL DEFAULT now() "
        ")"
    )
    # 投递路径的第一跳是"按事件找会话"，所以按 thread_id 建索引；
    # 反向（"这个会话订了哪些 thread"）用于展示与清理。
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_thread_subscriptions_by_thread "
        "ON public.thread_subscriptions (thread_id, status)"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_thread_subscriptions_by_session "
        "ON public.thread_subscriptions (session_id, status)"
    )
    # 同一 thread 不该对同一会话重复订阅：重复行会让一次事件投两遍，
    # 而投递表的幂等键是按 (thread_id, event_key) 设计的，挡不住这种重复。
    op.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS ux_thread_subscriptions_active "
        "ON public.thread_subscriptions (thread_id, session_id) "
        "WHERE status = 'active'"
    )

    op.execute(
        "CREATE TABLE IF NOT EXISTS public.thread_deliveries "
        "( "
        "    thread_id  varchar(36)  NOT NULL REFERENCES public.threads (id) ON DELETE CASCADE, "
        "    event_key  varchar(128) NOT NULL, "
        "    session_id varchar(36)  NOT NULL, "
        "    turn_id    varchar(36)  NOT NULL DEFAULT '', "
        "    status     varchar(16)  NOT NULL, "
        "    created_at timestamptz  NOT NULL DEFAULT now(), "
        "    PRIMARY KEY (thread_id, event_key) "
        ")"
    )
    # 收尾时 drain 队列要"按会话捞最早的待处理"，这条是它的扫描路径。
    # 部分索引只收 queued：running/done 的行数会远大于待处理，不该进同一条索引。
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_thread_deliveries_pending "
        "ON public.thread_deliveries (session_id, created_at) "
        "WHERE status = 'queued'"
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS public.ix_thread_deliveries_pending")
    op.execute("DROP TABLE IF EXISTS public.thread_deliveries")
    op.execute("DROP INDEX IF EXISTS public.ux_thread_subscriptions_active")
    op.execute("DROP INDEX IF EXISTS public.ix_thread_subscriptions_by_session")
    op.execute("DROP INDEX IF EXISTS public.ix_thread_subscriptions_by_thread")
    op.execute("DROP TABLE IF EXISTS public.thread_subscriptions")
    op.execute("DROP INDEX IF EXISTS public.ix_threads_owner")
    op.execute("DROP INDEX IF EXISTS public.threads_idem")
    op.execute("DROP TABLE IF EXISTS public.threads")
