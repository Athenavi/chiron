"""租户邀请码：注册时据邀请码决定 tenant_id

Revision ID: 0011_tenant_invites
Revises: 0010_knowledge_chunks_fts_index
Create Date: 2026-10-10

背景：此前注册/登录/邮件登录/SSO **一律**把 `tenant_id` 写成
`db.DefaultTenantID`（见 internal/api/auth.go 的 Register/Login 等），于是平台在数据
模型上支持多租户、在身份链路上却恒为单租户 —— `tenants` 表只能由后台
（admin_ops.go 的 CreateTenant）创建，普通用户永远进不去。

本迁移引入**邀请码**作为租户归属入口：注册请求携带 `invite_code` 时，归属到该码
对应的租户；不携带则维持 DefaultTenantID（存量单租户行为不变）。

设计取舍：

* `max_uses = 0` 表示**不限次数**（默认），便于"一个租户一个长期邀请码"；
* `expires_at` 可空 —— NULL 表示永不过期；
* `uses` 在注册事务内自增（与 users 插入同一事务，避免"码被消费但用户没建成"）；
* 不做软删除：撤销邀请码直接 DELETE 该行。

> 新增迁移后请复核 README 的「数据库迁移」段：启动时 `CheckSchemaVersion`
> 会把 head 与库中 alembic_version 比对，不一致将拒绝启动。
"""

from typing import Sequence, Union

from alembic import op

revision: str = "0011_tenant_invites"
down_revision: Union[str, Sequence[str], None] = "0010_knowledge_chunks_fts_index"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS public.tenant_invites (
            code character varying(64) PRIMARY KEY,
            tenant_id character varying(36) NOT NULL,
            created_by character varying(36),
            max_uses integer NOT NULL DEFAULT 0,
            uses integer NOT NULL DEFAULT 0,
            expires_at timestamp with time zone,
            created_at timestamp with time zone NOT NULL DEFAULT NOW()
        )
        """
    )
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS ix_tenant_invites_tenant
            ON public.tenant_invites (tenant_id)
        """
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS public.ix_tenant_invites_tenant")
    op.execute("DROP TABLE IF EXISTS public.tenant_invites")
