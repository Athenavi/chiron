"""conversation_shares.expires_at 回填 + 列默认值：分享链接不再"永久有效"

Revision ID: 0017_conversation_shares_expires_backfill
Revises: 0016_ent_array_columns_text
Create Date: 2026-10-18

``0013`` 加上 ``expires_at`` 时刻意让存量行留 NULL，读侧把 NULL 解释成"永不过期"，
以免加固动作把用户已经发出去的链接**当场**打断。代价是：那条"永久有效"的口子一直没闭上
—— 而 ``internal/api/share.go`` 的 ``PublicGet``（免鉴权的公开读取路径）当时压根没过滤
``expires_at``，连新建的 30 天 TTL 也没生效。

本次迁移配合 Go 侧的读侧收紧，把语义改成**一律有过期时间**：

1. 存量 NULL 行统一设为 ``NOW() + 30 天``（与 ``shareTTL`` 同长）。
   刻意不按 ``created_at + 30 天`` 计算：那会让 upgrade 之前满 30 天的分享**立即**失效，
   而这次变更的目的不是通知所有存量链接拥有者"你的链接今起打不开"。给一个统一的
   30 天宽限窗口，既闭上了永久公开的洞，也留出可预期的退场时间。
2. 列默认值设为 ``NOW() + interval '30 days'``：任何绕过应用层写入的 INSERT（运维脚本、
   未来的新代码路径）也不会再造出"永不过期"的行。

downgrade 只回退默认值，不清空 ``expires_at``：回填后已无法区分"原本就有 TTL"与"本次
补上的 TTL"，反向清空反而会把新建分享的有效期一起抹掉。

> 新增迁移后 head 前移：库未 stamp 到本 revision 时网关会拒绝启动
> （``ALLOW_SCHEMA_DRIFT=true`` 可放行）。生产迁移见 ``requirements-migrate.txt``。
"""

from typing import Sequence, Union

from alembic import op

revision: str = "0017_conversation_shares_expires_backfill"
down_revision: Union[str, Sequence[str], None] = "0016_ent_array_columns_text"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute(
        """
        UPDATE public.conversation_shares
           SET expires_at = NOW() + interval '30 days'
         WHERE expires_at IS NULL
        """
    )
    op.execute(
        "ALTER TABLE public.conversation_shares "
        "ALTER COLUMN expires_at SET DEFAULT (NOW() + interval '30 days')"
    )


def downgrade() -> None:
    op.execute(
        "ALTER TABLE public.conversation_shares "
        "ALTER COLUMN expires_at DROP DEFAULT"
    )
