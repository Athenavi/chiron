"""用户级 MFA / TOTP：users 表新增三列。

**为什么需要**：企业采购安全问卷几乎必问「是否支持双因子认证」，而此前全仓
没有任何 TOTP/MFA 实现 —— 密码是唯一因子，一旦泄露即账号失守。

列的取舍：
- `totp_secret_enc`：**加密后**再落库（AES-GCM，密钥沿用 `ENT_OIDC_SECRET_KEY`
  或 `APP_SECRET`）。TOTP 密钥等价于"永久密码"——拿到它就能无限生成有效验证码，
  因此不能像普通配置那样明文存。加密用 `internal/auth.EncryptAESGCM`。
- `totp_enabled`：**独立于 secret 是否存在**。用户在"已生成密钥但尚未用一次
  验证码确认"的中间态（扫码后关掉页面）时，secret 已有值但绝不能算已启用 ——
  否则他下次登录会被要求输入一个他从未成功配置过的验证码，直接把自己锁在门外。
  DEFAULT false 让存量用户行为完全不变。
- `totp_recovery_hashes`：备用码的**哈希**（不是明文，也不是加密 —— 备用码
  只需比对，不需要还原）。用 jsonb 而非 text[]：既有代码里 jsonb 的处理路径
  更完整，且长度不固定时不用管 PG 数组的转义。

`totp_confirmed_at` 不单独建列：`totp_enabled = true` 即代表"已确认过一次"，
再存一个时间戳只是重复表达（要审计"何时启用"可以查 audit_logs 的
`mfa.enabled` 事件）。

Revision ID: 0017_user_mfa
Revises: 0016_free_package_unique_claim
"""

from typing import Sequence, Union

from alembic import op

revision: str = "0017_user_mfa"
down_revision: Union[str, Sequence[str], None] = "0016_free_package_unique_claim"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # 三列都可空/有默认：存量用户（包括所有既有账号）行为必须逐字不变。
    op.execute(
        "ALTER TABLE public.users ADD COLUMN IF NOT EXISTS totp_secret_enc text;"
    )
    op.execute(
        "ALTER TABLE public.users ADD COLUMN IF NOT EXISTS totp_enabled boolean "
        "NOT NULL DEFAULT false;"
    )
    op.execute(
        "ALTER TABLE public.users ADD COLUMN IF NOT EXISTS totp_recovery_hashes jsonb;"
    )


def downgrade() -> None:
    op.execute("ALTER TABLE public.users DROP COLUMN IF EXISTS totp_recovery_hashes;")
    op.execute("ALTER TABLE public.users DROP COLUMN IF EXISTS totp_enabled;")
    op.execute("ALTER TABLE public.users DROP COLUMN IF EXISTS totp_secret_enc;")
