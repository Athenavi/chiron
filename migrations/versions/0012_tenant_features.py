"""租户功能开关：SSO 灰度与自助开通

Revision ID: 0012_tenant_features
Revises: 0011_tenant_invites
Create Date: 2026-10-11

背景：SSO provider 现在能按租户创建（见 `internal/api/sso_handler.go` 的 `CreateProvider`），
但**没有"平台是否允许该租户用 SSO"这一层** —— 任何租户 admin 都能自行开通。本迁移引入
租户级开关作为**灰度载体**：

* ``sso_enabled``      —— 该租户是否启用 SSO（登录页是否显示入口、登录是否放行）；
* ``sso_self_service`` —— 该租户 admin 能否**自行创建** provider（false ⇒ 仅平台 owner 可建）。

**默认全关**（无记录 = 关闭）：这是灰度语义 —— 新租户不会因为"没配"而自动开放，
由平台 owner 在后台逐个租户放开。

设计取舍：

* **不设 FK 到 tenants**：默认租户的 id 是固定 UUID，且"无记录 = 全关"是刻意语义，
  不必为每个租户预插一行；
* 不做软删除：撤销开关就是把布尔值改回 false。

> 新增迁移后请复核 README 的「数据库迁移」段：启动时 `CheckSchemaVersion` 会把 head 与
> 库中 alembic_version 比对，不一致将拒绝启动（`ALLOW_SCHEMA_DRIFT=true` 可放行）。
"""

from typing import Sequence, Union

from alembic import op

revision: str = "0012_tenant_features"
down_revision: Union[str, Sequence[str], None] = "0011_tenant_invites"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS public.tenant_features (
            tenant_id        character varying(36) PRIMARY KEY,
            sso_enabled      boolean NOT NULL DEFAULT false,
            sso_self_service boolean NOT NULL DEFAULT false,
            updated_at       timestamp with time zone NOT NULL DEFAULT NOW()
        )
        """
    )


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS public.tenant_features")
