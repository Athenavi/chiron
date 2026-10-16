"""ent_roles.permissions：varchar → text[]

Revision ID: 0015_ent_roles_permissions_array
Revises: 0014_harness_profiles
Create Date: 2026-10-16

背景（线上故障：后台管理页不断弹 ``insufficient permissions``）：

``ent_roles.permissions`` 在库中被建成了 ``character varying(255)``（见
``0001_authoritative_baseline`` 与 ``migrations/sql/init.sql``），但整个 Go 侧把它当
**PostgreSQL text[] 数组**使用：

* ``internal/enterprise/rbac.go`` 的 ``LoadEffectivePerms`` 用
  ``unnest(COALESCE(r.permissions, '{}'))`` 聚合权限点；
* ``internal/api/ent_identity_handler.go`` 的 ``scanRole`` / ``CreateRole`` / ``UpdateRole``
  以 ``[]string`` 读（``COALESCE(r.permissions, '{}')``）写（``permissions = $4``）。

列是标量 varchar 时，PG 直接报
``function unnest(character varying) does not exist (SQLSTATE 42883)`` ⇒
``LoadEffectivePerms`` 出错 ⇒ ``AllowedByEntOrLegacy`` 走 fail-close 返回 false ⇒
所有 ``RequireEntPerm(...)`` 保护的企业后台接口（``/v1/ent/roles``、
``/v1/ent/sso/providers``、``/v1/ent/sms/config``、``/v1/ent/mail/config``、
``/v1/ent/model-policies``、``/v1/ent/captcha/config`` …）一律 403 ⇒ 前端并发拉取这些
接口时**逐个弹窗**。

根因是 ORM 生成器（``scripts/generate_orm_models.py``）不认识 ``type: array``，把
``configs/orm/V1/models.yaml`` 里声明的数组字段降级成了 ``String(255)``，于是
「yaml 意图 = 数组」与「迁移 / baseline = varchar」长期漂移。本迁移先把**被代码实际
当作数组使用**的 ``ent_roles.permissions`` 纠正为 ``text[]``。

``USING`` 转换对存量数据保持宽容：NULL / 空串 / ``[]`` / ``{}`` ⇒ 空数组；PG 数组字面量
``{a,b}`` 原样解析；JSON 数组 ``["a","b"]`` 去掉方括号与引号后按逗号切分；其余按逗号切分
兜底。注意 PostgreSQL 禁止在 ``ALTER ... USING`` 中使用子查询
（``cannot use subquery in transform expression``），故这里不做严格 JSON 解析 —— 实测该表
在修复前无法被写入（``INSERT ... permissions = $4`` 传 ``[]string`` 到 varchar 列会直接报类型
错），存量数据为空或仅 ``[]`` / ``{}``，宽容转换已足够。

> 同一根因还波及若干被 Go 侧按数组读写的列（``conversation_shares.message_ids`` 用
> ``array_length``、``ent_oidc_providers.scopes``、``ent_model_policies.allowed_models``
> 等），需逐列确认语义后另行迁移（``uploads.chunks_received`` 例外：Go 侧按 JSON 文本
> 读写，不应改为数组）。本迁移只覆盖触发本次故障的 ``ent_roles.permissions``。

> 新增迁移后 head 前移：库未 stamp 到本 revision 时网关会拒绝启动
> （``ALLOW_SCHEMA_DRIFT=true`` 可放行）。生产迁移见 ``requirements-migrate.txt``。
"""

from typing import Sequence, Union

from alembic import op

revision: str = "0015_ent_roles_permissions_array"
down_revision: Union[str, Sequence[str], None] = "0014_harness_profiles"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute(
        r"""
        ALTER TABLE public.ent_roles
            ALTER COLUMN permissions TYPE text[]
            USING (
                CASE
                    WHEN permissions IS NULL OR btrim(permissions) = '' THEN ARRAY[]::text[]
                    WHEN btrim(permissions) IN ('[]', '{}', '[ ]') THEN ARRAY[]::text[]
                    WHEN left(btrim(permissions), 1) = '{' THEN btrim(permissions)::text[]
                    WHEN left(btrim(permissions), 1) = '[' THEN string_to_array(
                        btrim(replace(replace(replace(btrim(permissions), '[', ''), ']', ''), '"', '')),
                        ','
                    )
                    ELSE string_to_array(btrim(permissions), ',')
                END
            )
        """
    )
    op.execute(
        "ALTER TABLE public.ent_roles ALTER COLUMN permissions SET DEFAULT ARRAY[]::text[]"
    )


def downgrade() -> None:
    op.execute("ALTER TABLE public.ent_roles ALTER COLUMN permissions DROP DEFAULT")
    op.execute(
        r"""
        ALTER TABLE public.ent_roles
            ALTER COLUMN permissions TYPE character varying(255)
            USING (
                CASE
                    WHEN permissions IS NULL THEN NULL
                    ELSE left(array_to_json(permissions)::text, 255)
                END
            )
        """
    )
