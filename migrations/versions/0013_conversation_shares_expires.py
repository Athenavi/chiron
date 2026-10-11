"""conversation_shares.expires_at：分享链接有效期

Revision ID: 0013_conversation_shares_expires
Revises: 0012_tenant_features
Create Date: 2026-10-10

分享链接此前只有 `revoked_at`、没有有效期 —— 一旦生成就**永久可访问**
（`internal/api/share.go` 的 `activeShare` 只过滤 `revoked_at IS NULL`）。这里补
`expires_at`：

* 新建分享一律写入 `NOW() + 30 天`（见 share.go 的 `shareTTL`）；
* 存量行的 `expires_at` 为 NULL，读侧语义是"永不过期" —— 刻意保持存量分享可用，
  不因本次加固而静默失效。若要一并给存量分享设期限，可另行执行
  `UPDATE conversation_shares SET expires_at = created_at + interval '30 days'
   WHERE expires_at IS NULL AND revoked_at IS NULL`。

``IF NOT EXISTS``：与既有迁移风格一致。

> 新增迁移后请复核 README 的「数据库迁移」段：库未 stamp 到本 revision 会拒绝启动
> （`ALLOW_SCHEMA_DRIFT=true` 可放行）。
"""

from typing import Sequence, Union

from alembic import op

revision: str = "0013_conversation_shares_expires"
down_revision: Union[str, Sequence[str], None] = "0012_tenant_features"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute(
        "ALTER TABLE public.conversation_shares "
        "ADD COLUMN IF NOT EXISTS expires_at timestamptz"
    )


def downgrade() -> None:
    op.execute("ALTER TABLE public.conversation_shares DROP COLUMN IF EXISTS expires_at")
