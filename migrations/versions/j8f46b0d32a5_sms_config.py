"""补齐短信发信配置表 ent_sms_config

Revision ID: j8f46b0d32a5
Revises: i7e35a9c21f4
Create Date: 2026-02-06

为什么需要这条迁移：``internal/api/sms_handler.go`` 一直在读写 ``ent_sms_config``
（短信验证码登录、手机号绑定与后台「短信配置」），但这张表既不在 ``init.sql``，
也不在任何历史迁移里 —— 也就是说**新建环境从来就建不出它**，短信配置一旦保存或发码，
就会以 ``relation "ent_sms_config" does not exist`` 失败。这里把 DDL 补上。

表结构与代码契约为准（不是反过来）：

* ``tenant_id`` 作主键 —— 单租户单行，同时满足 ``ON CONFLICT (tenant_id)`` 的 upsert
  （该冲突推断要求唯一约束，主键即可）；
* **没有 id 列**：``UpdateConfig`` 的 INSERT 不写 id，凭空加一列会要求默认值才插得进去；
* 除 ``endpoint`` 外全部 NOT NULL + 默认值：``loadConfig`` 把这些列 ``Scan`` 进
  **非指针** Go 变量，NULL 会让 pgx 直接报错（``endpoint`` 是 ``*string``，故允许 NULL）；
* ``created_at`` / ``updated_at`` 带 ``default now()``：INSERT 不写这两列
  （只有 DO UPDATE 里写 ``updated_at``），没有默认值时间戳就永远是空。

``IF NOT EXISTS``：若某些环境已手工建过此表，本迁移不覆盖它（保持既存结构不动）。
"""

from typing import Sequence, Union

from alembic import op

revision: str = "j8f46b0d32a5"
down_revision: Union[str, Sequence[str], None] = "i7e35a9c21f4"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS ent_sms_config (
            tenant_id             varchar(36) NOT NULL PRIMARY KEY REFERENCES tenants,
            provider              varchar(32)  NOT NULL DEFAULT 'aliyun',
            sign_name             varchar(64)  NOT NULL DEFAULT '',
            template_id           varchar(64)  NOT NULL DEFAULT '',
            access_key_id         varchar(256) NOT NULL DEFAULT '',
            secret_enc            text         NOT NULL DEFAULT '',
            endpoint              varchar(512),
            code_ttl_seconds      integer      NOT NULL DEFAULT 300,
            send_interval_seconds integer      NOT NULL DEFAULT 60,
            daily_limit           integer      NOT NULL DEFAULT 10,
            login_enabled         boolean      NOT NULL DEFAULT false,
            auto_register         boolean      NOT NULL DEFAULT false,
            enabled               boolean      NOT NULL DEFAULT false,
            created_at            timestamp    DEFAULT now(),
            updated_at            timestamp    DEFAULT now()
        )
        """
    )


def downgrade() -> None:
    # 仅回滚结构：短信通道配置（含 AccessKeySecret 密文）随之丢失，需要时重新在后台填写。
    op.execute("DROP TABLE IF EXISTS ent_sms_config")
