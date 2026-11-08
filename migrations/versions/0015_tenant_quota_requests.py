"""租户配额管理（申请 → 审批）+ ent_quota_pools 唯一约束。

背景：`ent_quota_pools` 的执行侧（token 预留 / credits 硬拦 / 并发 / 存储）早已完备，
但**没有任何管理入口**——管理员只能手工插表。本迁移补上「租户查看 → 申请调整 →
平台审批」这条链路的持久化，并修掉一个会让写入路径直接报错的结构缺口。

关键点 1：`ent_quota_pools` 只有主键，**没有** `(tenant_id, resource_type, period)`
唯一约束。配额池的 upsert 必须靠 `ON CONFLICT` 才有并发安全，而没有唯一索引时
PostgreSQL 会报 `42P10 there is no unique or exclusion constraint matching the
ON CONFLICT specification`（与 0014 的 `ent_captcha_config` 完全同一个坑）。
这里用**表达式索引**而不是普通唯一索引：`resource_type` / `period` 两列都可为 NULL，
而 NULL 之间互不相等 —— 普通唯一索引挡不住重复的 `(tenant_id, NULL, NULL)`。

关键点 2：申请表只存**快照 + 决策**，不存"审批后应该成为多少"的推导。
`requested_amount` 是申请人要求的目标值，审批通过时直接写进 `ent_quota_pools`，
两者不建立外键/触发器关系 —— 否则"改配额"和"改申请"会变成同一份状态的两个副本。

Revision ID: 0015_tenant_quota_requests
Revises: 0014_captcha_config_unique_tenant
"""

from typing import Sequence, Union

from alembic import op

revision: str = "0015_tenant_quota_requests"
down_revision: Union[str, Sequence[str], None] = "0014_captcha_config_unique_tenant"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # 配额池作用域唯一：同一租户的同一资源、同一周期只能有一行。
    op.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS ux_ent_quota_pools_scope "
        "ON public.ent_quota_pools "
        "(tenant_id, COALESCE(resource_type, ''), COALESCE(period, ''));"
    )

    op.execute(
        """CREATE TABLE IF NOT EXISTS public.ent_quota_requests (
    id character varying(36) NOT NULL,
    tenant_id smallint NOT NULL,
    resource_type character varying(20) NOT NULL,
    period character varying(10) NOT NULL DEFAULT 'monthly',
    current_amount bigint NOT NULL DEFAULT 0,
    requested_amount bigint NOT NULL,
    reason text,
    status character varying(16) NOT NULL DEFAULT 'pending',
    requested_by character varying(36),
    reviewed_by character varying(36),
    reviewed_at timestamp with time zone,
    review_note text,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT ent_quota_requests_pkey PRIMARY KEY (id),
    CONSTRAINT ent_quota_requests_status_check
        CHECK (status IN ('pending', 'approved', 'rejected')),
    CONSTRAINT ent_quota_requests_tenant_id_fkey
        FOREIGN KEY (tenant_id) REFERENCES public.tenants(id)
);"""
    )
    # 租户视图按 tenant 倒序翻历史；平台视图按 status 过滤待审。
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_ent_quota_requests_tenant "
        "ON public.ent_quota_requests (tenant_id, created_at DESC);"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_ent_quota_requests_status "
        "ON public.ent_quota_requests (status, created_at DESC);"
    )


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS public.ent_quota_requests;")
    op.execute("DROP INDEX IF EXISTS public.ux_ent_quota_pools_scope;")
