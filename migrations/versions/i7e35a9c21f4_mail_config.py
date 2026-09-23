"""邮件发信配置表 ent_mail_config

Revision ID: i7e35a9c21f4
Revises: h5c13e7a60d2
Create Date: 2026-02-06

单租户单行（``tenant_id`` UNIQUE），承载后台「邮件配置」的全部内容：

* 发信通道 —— ``smtp``（通用 SMTP：host/port/用户名/密码/TLS 模式）或
  ``qingchen``（晴辰云邮 HTTP API，契约见 docs/mail.md）；
* 凭据密文 —— 与 ``ent_sms_config.secret_enc``、``ent_oidc_providers.client_secret_enc``
  同口径，用 ``ENT_OIDC_SECRET_KEY`` 派生的 AES-256-GCM 密钥加密（internal/auth.EncryptAESGCM）；
* 发件身份与站点信息 —— from / reply_to / site_name / app_base_url（重置链接的基址）；
* 邮件模板 —— code/welcome/reset 三套 subject+body，留空即回落到代码内置默认模板；
* 能力开关与限流 —— 邮箱登录、注册邮箱验证、密码重置、欢迎邮件、验证码 TTL、冷却、日限、超时。

为什么需要 ``UNIQUE (tenant_id)``：保存路径用 ``INSERT ... ON CONFLICT (tenant_id) DO UPDATE``
做 upsert（与 ent_sms_config 相同的语义），而 ON CONFLICT 只能命中**唯一约束**，
不能命中普通索引 —— 缺了它保存会直接报 42P10。
"""

from typing import Sequence, Union

from alembic import op

revision: str = "i7e35a9c21f4"
down_revision: Union[str, Sequence[str], None] = "h5c13e7a60d2"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS ent_mail_config (
            id                    varchar(36) NOT NULL PRIMARY KEY,
            tenant_id             varchar(36) NOT NULL UNIQUE REFERENCES tenants,
            provider              varchar(32),
            enabled               boolean NOT NULL DEFAULT false,
            smtp_host             varchar(255),
            smtp_port             integer,
            smtp_username         varchar(255),
            smtp_password_enc     text,
            smtp_security         varchar(16),
            smtp_skip_verify      boolean NOT NULL DEFAULT false,
            api_base_url          varchar(512),
            api_key_enc           text,
            api_channel_id        integer,
            api_template_id       integer,
            from_address          varchar(255),
            from_name             varchar(128),
            reply_to              varchar(255),
            site_name             varchar(128),
            app_base_url          varchar(512),
            login_enabled         boolean NOT NULL DEFAULT false,
            register_verify       boolean NOT NULL DEFAULT false,
            auto_register         boolean NOT NULL DEFAULT false,
            reset_enabled         boolean NOT NULL DEFAULT false,
            welcome_enabled       boolean NOT NULL DEFAULT true,
            code_subject          varchar(255),
            code_body             text,
            welcome_subject       varchar(255),
            welcome_body          text,
            reset_subject         varchar(255),
            reset_body            text,
            code_ttl_seconds      integer NOT NULL DEFAULT 300,
            send_interval_seconds integer NOT NULL DEFAULT 60,
            daily_limit           integer NOT NULL DEFAULT 10,
            timeout_seconds       integer NOT NULL DEFAULT 30,
            created_at            timestamp,
            updated_at            timestamp
        )
        """
    )


def downgrade() -> None:
    # 仅回滚结构：邮件通道配置（含凭据密文）随之丢失，需要时重新在后台填写。
    op.execute("DROP TABLE IF EXISTS ent_mail_config")
