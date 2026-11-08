"""给 varchar(36) 主键补 UUID 默认值、给缺省时间戳补 now()。

**为什么需要这条迁移**：基线 0001 是从 pg_dump 导出的，`id character varying(36) NOT NULL`
这些列当时**没有** `DEFAULT`（表结构里的 id 由应用层显式生成）。但代码里有一批插入语句
只写了业务列，没有写 `id` —— 于是运行期直接撞非空约束：

    ERROR: null value in column "id" of relation "ent_groups" violates not-null constraint

已确认的受害者：`POST /v1/ent/groups`（群组）、`POST /v1/admin/domains`（域名），
以及 `ent_roles` / `ent_catalog_items` / `ent_user_identities` / `ent_captcha_config` /
`ent_tenant_policies` / `ent_model_policies` 的创建路径（同类写法，只是没人点到）。

在数据库层补默认值比逐处补 SQL 更可靠：应用里仍可显式传 id（显式值优先于默认值），
漏传时也不会再 500。`gen_random_uuid()` 自 PostgreSQL 13 起是内置函数，本仓库要求 14+
（实测开发库为 18.1），因此不依赖 pgcrypto 扩展。

`tenants.created_at` / `domains.created_at` / `ent_groups.created_at` 同样没有默认值，
插入后是 NULL。这两个列表接口用 `time.Time` 扫描该列，NULL 会让 `rows.Scan` 报错，
而调用方对扫描失败是**静默跳过行**的写法 —— 表现就是"新建成功但列表里什么都没有"。
这里补默认值并回填存量 NULL（回填成迁移时刻，无法还原真实创建时间，但不回填就永远读不出）。

Revision ID: 0003_uuid_defaults_and_timestamps
Revises: 0002_messages_attachments
"""

from typing import Sequence, Union

from alembic import op

revision: str = "0003_uuid_defaults_and_timestamps"
down_revision: Union[str, Sequence[str], None] = "0002_messages_attachments"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

#: 这些表的 created_at 会直接出现在管理后台列表里，NULL 会导致整行读不出来。
_TIMESTAMP_TABLES = ("tenants", "domains", "ent_groups")


def upgrade() -> None:
    # 1) 所有 public 表里「varchar(36) 且没有默认值」的 id 列补 UUID 默认值。
    #    用 DO 块遍历而不是硬编码表名：基线与后续迁移新增的同类表自动覆盖，
    #    也不会因为手抄表名漏掉一张。
    op.execute(
        """
        DO $$
        DECLARE r record;
        BEGIN
            FOR r IN
                SELECT c.table_name
                  FROM information_schema.columns c
                  JOIN information_schema.tables t
                    ON t.table_schema = c.table_schema
                   AND t.table_name = c.table_name
                 WHERE c.table_schema = 'public'
                   AND c.column_name = 'id'
                   AND c.data_type = 'character varying'
                   AND c.character_maximum_length = 36
                   AND c.column_default IS NULL
                   AND t.table_type = 'BASE TABLE'
            LOOP
                EXECUTE format(
                    'ALTER TABLE public.%I ALTER COLUMN id SET DEFAULT gen_random_uuid()::text',
                    r.table_name
                );
            END LOOP;
        END $$;
        """
    )

    # 2) 列表页要展示的 created_at：补默认值，并回填存量 NULL。
    for table in _TIMESTAMP_TABLES:
        op.execute(
            f"ALTER TABLE public.{table} ALTER COLUMN created_at SET DEFAULT now()"
        )
        op.execute(
            f"UPDATE public.{table} SET created_at = now() WHERE created_at IS NULL"
        )


def downgrade() -> None:
    # 只撤销默认值；已回填的时间戳不回滚（回滚成 NULL 只会让列表再次读不出来，
    # 且原始值本来就不可知）。
    for table in _TIMESTAMP_TABLES:
        op.execute(
            f"ALTER TABLE public.{table} ALTER COLUMN created_at DROP DEFAULT"
        )
    op.execute(
        """
        DO $$
        DECLARE r record;
        BEGIN
            FOR r IN
                SELECT c.table_name
                  FROM information_schema.columns c
                 WHERE c.table_schema = 'public'
                   AND c.column_name = 'id'
                   AND c.data_type = 'character varying'
                   AND c.character_maximum_length = 36
                   AND c.column_default = 'gen_random_uuid()::text'
            LOOP
                EXECUTE format(
                    'ALTER TABLE public.%I ALTER COLUMN id DROP DEFAULT',
                    r.table_name
                );
            END LOOP;
        END $$;
        """
    )
