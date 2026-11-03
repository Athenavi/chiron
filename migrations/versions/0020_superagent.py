"""superagent：接管运行态、用户级定时任务、回合来源标记

配置（用户想要什么）存 `users.settings.superagent_takeover`，零迁移；
本文件只建**运行态**，它必须跨副本可见、按时间自动失效、可审计。

`superagent_takeovers`
----------------------
`ux_superagent_active` 这条**部分唯一索引**是并发正确性的第一道闸：两个浏览器
或两个副本同时点"启用"时由数据库直接拒掉第二个，应用层不必再加锁。

时间列一律用 `timestamptz`。到期判定是"拿 PG 的 now() 比"，若用无时区的
`timestamp`，容器时区一变，同一个 `ends_at` 就换了含义。

`superagent_schedules`
----------------------
不复用 `cron_jobs`：那张表按 `tenant_id` 过滤，普通用户一调就是整个租户的任务；
且它的 `task` 直打引擎、不过会话锁，与"接管必须排队进同一会话"冲突。

`turns.origin`
--------------
标在**回合**而非消息上：`messages.source` 只有 `SaveUserMessage` 会写，
assistant 产出的两条路径都不带，标在消息上会漏掉真正的接管产物。
`DEFAULT ''` 让历史行自动为空串，PG 11+ 属元数据操作，不重写表。

Revision ID: 0020_superagent
Revises: 0019_session_folds
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0020_superagent"
down_revision: str | Sequence[str] | None = "0019_session_folds"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        "CREATE TABLE IF NOT EXISTS public.superagent_takeovers "
        "( "
        "    id           varchar(36) NOT NULL PRIMARY KEY, "
        "    tenant_id    smallint    NOT NULL REFERENCES public.tenants, "
        "    user_id      varchar(36) NOT NULL REFERENCES public.users, "
        "    session_id   varchar(36) NOT NULL REFERENCES public.sessions, "
        "    status       varchar(16) NOT NULL, "
        "    started_at   timestamptz NOT NULL DEFAULT now(), "
        "    ends_at      timestamptz NOT NULL, "
        "    stop_reason  varchar(64), "
        "    token_spent  bigint      NOT NULL DEFAULT 0, "
        "    token_cap    bigint      NOT NULL DEFAULT 0, "
        "    lease_token  varchar(64), "
        "    lease_at     timestamptz, "
        "    tick_seconds integer     NOT NULL DEFAULT 180, "
        "    max_rounds   integer     NOT NULL DEFAULT 0, "
        "    rounds       integer     NOT NULL DEFAULT 0, "
        "    last_tick_at timestamptz, "
        "    last_status  varchar(16), "
        "    stopping_at  timestamptz "
        ")"
    )
    # token_cap 与 token_spent 必须分开：前者是用户设的硬上限，后者是已用量。
    # 合并成一列会让"预算"在开启瞬间就等于"已消耗"，接管一拍都跑不了。
    op.execute(
        "ALTER TABLE public.superagent_takeovers "
        "ADD COLUMN IF NOT EXISTS token_cap bigint NOT NULL DEFAULT 0"
    )
    # 同一会话同时只允许一个未终结的接管；stopping 也算，否则"正在停止"的
    # 那一小段窗口里能被再次开启，正是要防的并发态。
    op.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS ux_superagent_active "
        "ON public.superagent_takeovers (session_id) "
        "WHERE status IN ('active', 'stopping')"
    )
    # 列表与配额都按 (user_id, tenant_id) 收窄；配额查的是 status IN (...)。
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_superagent_takeovers_owner "
        "ON public.superagent_takeovers (user_id, tenant_id, status)"
    )

    op.execute(
        "CREATE TABLE IF NOT EXISTS public.superagent_schedules "
        "( "
        "    id              varchar(36)  NOT NULL PRIMARY KEY, "
        "    tenant_id       smallint     NOT NULL REFERENCES public.tenants, "
        "    user_id         varchar(36)  NOT NULL REFERENCES public.users, "
        "    name            varchar(128), "
        "    schedule        varchar(64)  NOT NULL, "
        "    window_seconds  integer      NOT NULL, "
        "    session_ids     jsonb        NOT NULL, "
        "    enabled         boolean      NOT NULL DEFAULT false, "
        "    last_run_at     timestamptz, "
        "    last_status     varchar(16), "
        "    run_lease_at    timestamptz, "
        "    run_lease_token varchar(64), "
        "    created_at      timestamptz  NOT NULL DEFAULT now(), "
        "    updated_at      timestamptz  NOT NULL DEFAULT now() "
        ")"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_superagent_schedules_owner "
        "ON public.superagent_schedules (user_id, tenant_id)"
    )
    # 租约查询要能快速捞出"带租约的启用任务"，与 cron_jobs 的扫描形态一致。
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_superagent_schedules_enabled "
        "ON public.superagent_schedules (enabled) WHERE enabled"
    )

    op.execute(
        "ALTER TABLE public.turns "
        "ADD COLUMN IF NOT EXISTS origin varchar(32) NOT NULL DEFAULT ''"
    )


def downgrade() -> None:
    op.execute("ALTER TABLE public.turns DROP COLUMN IF EXISTS origin")
    op.execute("DROP INDEX IF EXISTS public.ix_superagent_schedules_enabled")
    op.execute("DROP INDEX IF EXISTS public.ix_superagent_schedules_owner")
    op.execute("DROP TABLE IF EXISTS public.superagent_schedules")
    op.execute("DROP INDEX IF EXISTS public.ix_superagent_takeovers_owner")
    op.execute("DROP INDEX IF EXISTS public.ux_superagent_active")
    op.execute("DROP TABLE IF EXISTS public.superagent_takeovers")
