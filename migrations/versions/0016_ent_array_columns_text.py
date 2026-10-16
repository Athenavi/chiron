"""把被 Go 侧当作 text[] 使用的三列由 varchar 改为 text[]

Revision ID: 0016_ent_array_columns_text
Revises: 0015_ent_roles_permissions_array
Create Date: 2026-10-16

延续 ``0015`` 的同源问题：这些列在库里被建成 ``character varying(255)``，但 Go 侧按
**PostgreSQL text[] 数组**读写：

* ``conversation_shares.message_ids`` —— ``internal/api/share.go`` 以 ``[]string`` 写入
  （``INSERT ... $5``）、以 ``array_length(message_ids, 1)`` 计数、以 ``[]string`` 扫描读出；
  原注释即写着"``[]string`` 被 pgx 编码成数组字面量写进 varchar(255)，超过 255 字符即报
  value too long"（该修复此前被记作迁移 ``d3e5f7a9b1c4``，但仓库里并不存在这个迁移）。
* ``ent_oidc_providers.scopes`` —— ``internal/api/sso_handler.go`` 以 ``[]string`` 读写。
* ``ent_model_policies.allowed_models`` —— ``internal/api/ent_policy_handler.go`` 以
  ``[]string`` 读写（``INSERT/UPDATE ... allowed_models = $n``、``SELECT allowed_models``）。

列的**权威语义是数组**，标量 varchar 会让上面的读写以隐式转换/报错的方式失败。

背景说明（2026-10-16）：仓库原有的 ``configs/orm/V1/models.yaml`` → ``shared/models`` ORM
生成链路已整体移除（生成器不认识 ``type: array``，把数组字段生成为 ``String(255)``，正是本
类 schema 漂移的来源）；现在 schema 变更直接写迁移。``models.yaml`` 仅作 CI/CD 与 API 参考。

**刻意不改的列**（它们并非 PG 数组，改动会破坏现有实现）：

* ``media_assets.tags`` —— Go（``strings.Split/Join``）与 Python（``app/tools/media.py``）
  都按**逗号分隔字符串**读写；``0015`` 之前亦然。
* ``uploads.chunks_received`` —— Go 按 **JSON 文本**读写（``...::jsonb``）。
* ``kb_articles.tags`` · ``wiki_pages.tags`` · ``meeting_notes.participants`` ·
  ``admin_api_keys.allowed_models`` · ``admin_model_configs.fallback_chain`` —— Go/Python
  侧没有按数组使用的路径，保持现状，待真正用到时再按需迁移。

三张表在本次迁移的环境中均为空表；``USING`` 仍对存量数据保持宽容（NULL / 空串 / ``[]`` /
``{}`` ⇒ 空数组；``{a,b}`` 原样解析；``["a","b"]`` 去方括号引号后按逗号切分；其余按逗号切分）。
注意 PostgreSQL 禁止在 ``ALTER ... USING`` 里用子查询。

> 新增迁移后 head 前移：库未 stamp 到本 revision 时网关会拒绝启动
> （``ALLOW_SCHEMA_DRIFT=true`` 可放行）。生产迁移见 ``requirements-migrate.txt``。
"""

from typing import Sequence, Union

from alembic import op

revision: str = "0016_ent_array_columns_text"
down_revision: Union[str, Sequence[str], None] = "0015_ent_roles_permissions_array"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _to_text_array(table: str, column: str) -> None:
    """把 ``table.column`` 由 varchar 宽容地转为 text[] 并设空数组默认值。"""
    op.execute(
        rf"""
        ALTER TABLE public.{table}
            ALTER COLUMN {column} TYPE text[]
            USING (
                CASE
                    WHEN {column} IS NULL OR btrim({column}) = '' THEN ARRAY[]::text[]
                    WHEN btrim({column}) IN ('[]', '{{}}', '[ ]') THEN ARRAY[]::text[]
                    WHEN left(btrim({column}), 1) = '{{' THEN btrim({column})::text[]
                    WHEN left(btrim({column}), 1) = '[' THEN string_to_array(
                        btrim(replace(replace(replace(btrim({column}), '[', ''), ']', ''), '"', '')),
                        ','
                    )
                    ELSE string_to_array(btrim({column}), ',')
                END
            )
        """
    )
    op.execute(
        f"ALTER TABLE public.{table} ALTER COLUMN {column} SET DEFAULT ARRAY[]::text[]"
    )


def _to_varchar(table: str, column: str) -> None:
    """反向：text[] → varchar(255)（JSON 化并截断）。"""
    op.execute(f"ALTER TABLE public.{table} ALTER COLUMN {column} DROP DEFAULT")
    op.execute(
        rf"""
        ALTER TABLE public.{table}
            ALTER COLUMN {column} TYPE character varying(255)
            USING (
                CASE
                    WHEN {column} IS NULL THEN NULL
                    ELSE left(array_to_json({column})::text, 255)
                END
            )
        """
    )


def upgrade() -> None:
    _to_text_array("conversation_shares", "message_ids")
    _to_text_array("ent_oidc_providers", "scopes")
    _to_text_array("ent_model_policies", "allowed_models")


def downgrade() -> None:
    _to_varchar("ent_model_policies", "allowed_models")
    _to_varchar("ent_oidc_providers", "scopes")
    _to_varchar("conversation_shares", "message_ids")
