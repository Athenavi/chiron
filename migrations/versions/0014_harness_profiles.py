"""租户/用户级 harness 配置（P1-④）

Revision ID: 0014_harness_profiles
Revises: 0013_conversation_shares_expires
Create Date: 2026-10-12

背景：引擎已有 harness 塑形机制（`app/agent/harness_profile.py`，对位 deepagents 的
`HarnessProfile`），但注册表**只能由部署方在代码里注册** —— deepagents 同样如此。
本表把这一层下沉到**租户级 / 用户级**：

* ``user_id = ''`` ⇒ 租户级默认；
* 具体 ``user_id`` ⇒ 用户级，覆盖租户级；
* ``model_prefix`` ⇒ 只对前缀匹配的模型生效（``''`` = 该租户的所有模型）。

**默认关**（``enabled = false`` / 无记录 = 不生效）：这是"新机制默认什么都不做"的落点 ——
引入本表不该悄悄改变任何现有会话的 system prompt（读取方见 `app/agent/harness_store.py`）。

设计取舍：

* **不设 FK 到 tenants/users**：与 ``0012_tenant_features`` 同一取向（无记录 = 不生效，
  不必为每个租户预插一行）；
* ``system_prompt_suffix`` 用 ``text``，长度上限由**读取方**强制
  （`harness_store.MAX_SUFFIX_CHARS`）—— 放进 DB 约束会让"改上限"变成一次迁移。

> 新增迁移后请复核 README 的「数据库迁移」段：启动时 `CheckSchemaVersion` 会把 head
> 与库中 alembic_version 比对，不一致将拒绝启动（`ALLOW_SCHEMA_DRIFT=true` 可放行）。
"""

from typing import Sequence, Union

from alembic import op

revision: str = "0014_harness_profiles"
down_revision: Union[str, Sequence[str], None] = "0013_conversation_shares_expires"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS public.harness_profiles (
            tenant_id            character varying(36)  NOT NULL,
            user_id              character varying(64)  NOT NULL DEFAULT '',
            model_prefix         character varying(128) NOT NULL DEFAULT '',
            system_prompt_suffix text                   NOT NULL DEFAULT '',
            enabled              boolean                NOT NULL DEFAULT false,
            updated_at           timestamp with time zone NOT NULL DEFAULT NOW(),
            PRIMARY KEY (tenant_id, user_id, model_prefix)
        )
        """
    )


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS public.harness_profiles")
