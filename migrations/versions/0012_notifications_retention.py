"""通知表保留期清理所需的索引（T2）

背景（已核实）
--------------
`0011_notifications` 建了三个索引，**全部以 `user_id` 打头**：
`ux_notifications_user_dedupe (user_id, dedupe_key)`、
`idx_notifications_user_created (user_id, created_at DESC)`、
`idx_notifications_user_unread (user_id) WHERE read_at IS NULL`。

前缀不匹配 ⇒ 按 `created_at` **跨用户**删除（保留期清理的唯一查询形状）用不上
任何一个，会退化成全表扫描。而通知表是只增不减的：`quota_changed` 会按租户
owner/admin 数量成倍写入，越晚加索引，首次清理要扫的历史越多。

设计取舍
--------
1. **只加一个索引，不加任何列**。保留期是"从 `created_at` 起算多少天"，纯派生
   规则；加 `expires_at` 列会让每一行多一份可能与 `created_at` 不一致的真相，
   且改保留期就得回填全表。
2. **索引不带 WHERE**。这里刻意不写成 `WHERE read_at IS NULL`：清理**已读与未读
   一视同仁**（90 天没点开的通知，保留下去既不会被看也不会被清理），所以删除
   条件只看 `created_at`，部分索引反而让查询用不上它。
3. **不建 `(created_at, id)` 复合索引**。当前删除只按 `created_at < cutoff` 单条件
   过滤，不带游标分页；单列索引足够，复合索引只增加写放大。
4. **升级路径对已有数据是 O(n) 建索引**，但表刚上线、行数少；若将来数据量已大，
   应改为 `CREATE INDEX CONCURRENTLY`（不能在事务内执行，需单独迁移脚本）。

Revision ID: 0012_notifications_retention
Revises: 0011_notifications
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0012_notifications_retention"
down_revision: str | Sequence[str] | None = "0011_notifications"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # 保留期清理：`DELETE FROM notifications WHERE created_at < cutoff LIMIT n`。
    # 现有三个索引都以 user_id 打头，这个查询跨用户，故必须单独建。
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_notifications_created "
        "ON public.notifications USING btree (created_at)"
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS public.idx_notifications_created")
