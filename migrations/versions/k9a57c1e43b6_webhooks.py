"""补齐企业 Webhook 订阅表 ent_webhooks

Revision ID: k9a57c1e43b6
Revises: j8f46b0d32a5
Create Date: 2026-02-06

与 ent_sms_config 同一类缺口：``internal/api/ent_webhook_handler.go``（订阅的 CRUD）与
``internal/api/ent_webhook_dispatcher.go``（事件投递）一直在读写这张表，但仓库里
（``init.sql`` 与任何历史迁移）都没有它的 DDL —— 新建环境建不出它，后台配置 Webhook
订阅会以 ``relation "ent_webhooks" does not exist`` 失败。

列与类型以代码契约为准：

* ``event_types`` / ``retry_policy`` 用 **jsonb**：分发侧执行
  ``event_types::jsonb @> to_jsonb($2::text)`` 的包含匹配（jsonb 是自然类型，cast 无开销）；
  写入侧传 ``json.Marshal`` 得到的 ``[]byte``，pgx 的 JSON 编解码器直接接受字节切片；
* 读侧 ``id::text, tenant_id::text, event_types::text, retry_policy::text``：同类型 cast
  合法，jsonb→text 输出标准 JSON，Go 侧 ``json.Unmarshal`` 可解；
* 其余列 **NOT NULL + 默认值**：``scanWebhookRow`` 把它们全部扫进非指针 Go 变量
  （``string`` / ``bool`` / ``time.Time``），NULL 会让 pgx 报错；
* ``url`` 用 text：Create 只校验非空、不校验长度，用 varchar 反而会引入额外的 500 面；
* ``created_at`` / ``updated_at`` 带 ``now()`` 默认值：INSERT 不写这两列
  （只有 Update 里写 ``updated_at = NOW()``）。

``IF NOT EXISTS``：某些环境若已手工建过此表，本迁移不覆盖既存结构。
"""

from typing import Sequence, Union

from alembic import op

revision: str = "k9a57c1e43b6"
down_revision: Union[str, Sequence[str], None] = "j8f46b0d32a5"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS ent_webhooks (
            id           varchar(36)  NOT NULL PRIMARY KEY,
            tenant_id    varchar(36)  NOT NULL REFERENCES tenants,
            name         varchar(255) NOT NULL DEFAULT '',
            event_types  jsonb        NOT NULL DEFAULT '[]'::jsonb,
            url          text         NOT NULL DEFAULT '',
            secret       text         NOT NULL DEFAULT '',
            enabled      boolean      NOT NULL DEFAULT true,
            retry_policy jsonb        NOT NULL DEFAULT '{}'::jsonb,
            created_at   timestamp    NOT NULL DEFAULT now(),
            updated_at   timestamp    NOT NULL DEFAULT now()
        )
        """
    )
    # 投递侧按 (tenant_id, enabled) 过滤，建复合索引；event_types 若需要 GIN 索引，
    # 由发布流程按实际订阅量决定（小规模下收益有限）。
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_ent_webhooks_tenant_enabled ON ent_webhooks (tenant_id, enabled)"
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS ix_ent_webhooks_tenant_enabled")
    op.execute("DROP TABLE IF EXISTS ent_webhooks")
