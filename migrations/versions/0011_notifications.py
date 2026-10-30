"""站内通知中心（P2-3）

背景（已核实）
--------------
此前全仓**没有任何通知实现**：`rg 'notifications'` 只命中 `billing.go` 的
"async observer notification"（额度观察者，无存储）与 `middleware.go` 的跨实例
黑名单日志，都不是面向用户的通知。站内的"消息"先例是对话内的 notice 行，
它绑定 session，不能当通知中心用。

与此同时，事件源在 P2-2 系列之后才真正齐备：套餐可后台建、可售、权益会到期，
支付成功与配额变更都有确定的服务端写入点。因此这张表不是空转的基础设施。

设计取舍
--------
1. **去重键按 (user_id, dedupe_key) 建唯一索引，而不是只按 dedupe_key**。
   同一个事件可能同时通知多人（租户管理员改配额 → 通知租户内所有成员），
   若只按事件键唯一，第二个收件人的通知会被静默吞掉。键里带 user_id 之后，
   "同一事件对同一人只推一次"才是准确的语义。
   幂等判定靠插入撞 `23505` 跳过，**不做"先查后写"**：并发下两个请求都能查到
   "没有"，于是各插一条，用户收到重复推送。
2. **`tenant_id` 必须与 `tenants.id` 同类型（smallserial 底层即 smallint）**。
   基线 `0001:735` 是 `smallserial`，`users.tenant_id`（`0001:858`）是 `smallint`。
   若这里写成 `integer`，外键能建但类型不一致会让 JOIN 走不到索引，
   而这类问题在数据量小时完全看不出来。
3. **站内信不可关闭，只让用户关邮件**。站内是事实留痕（"我到底被通知过没有"），
   关掉它会让"用户声称没收到"变成无法查证的口径之争；退订诉求由邮件开关满足。
   因此 `user_notification_prefs` 只有 `email_enabled`，**没有 in_app_enabled**。
4. **偏好表不预置行，缺行即默认开启**。`email_enabled` 的默认值写在读路径上
   （`COALESCE(?, TRUE)`），而不是给每个用户 × 每种事件种一行 ——
   后者会让新增事件类型时必须补数据迁移，且存量用户全部漏发。
5. **`read_at` 用时间戳而非 `is_read` 布尔**。两者信息量不同：时间戳能回答
   "什么时候读的"，布尔只能回答"读没读"。代价是查询要写 `read_at IS NULL`，
   这已经被下面的部分索引覆盖。
6. **不存渲染后的 HTML**。标题/正文/链接存结构化字段，由前端按当前语言渲染；
   存 HTML 会让多语言切换后历史通知仍是旧语言，也会把 XSS 面拉进数据库。
   需要细节时用 `payload jsonb` 承载（如 `payment_id`、`days_before`）。
7. **邮件出口不写在本表**。发送成功与否是投递状态，不是通知本身；把
   `email_sent_at` 加进来就得处理重试与失败原因，属于另一类状态机。
   本期邮件是"尽力而为"的旁路：发送失败只记日志，站内信永远先落地。

Revision ID: 0011_notifications
Revises: 0010_seed_billing_packages
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0011_notifications"
down_revision: str | Sequence[str] | None = "0010_seed_billing_packages"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # ── 通知本体 ──
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS public.notifications
        (
            id         varchar(36) PRIMARY KEY,
            -- 与 tenants.id（smallserial）/ users.tenant_id（smallint）同类型，
            -- 见文件头取舍 2。
            tenant_id  smallint    NOT NULL REFERENCES public.tenants (id),
            user_id    varchar(36) NOT NULL REFERENCES public.users (id),
            -- 事件类型白名单由 Go 侧约束（见 internal/notify）：表里存字符串是为了
            -- 新增事件类型时不必改表结构。
            kind       varchar(64) NOT NULL,
            -- 幂等键：同一事件对同一用户只推一次（取舍 1）。
            dedupe_key varchar(255) NOT NULL,
            title      varchar(255) NOT NULL,
            body       text NOT NULL DEFAULT '',
            -- 站内跳转目标（如 /billing?tab=packages）。空串 = 不可点。
            link       varchar(512) NOT NULL DEFAULT '',
            -- 结构化细节，供前端按需展示；不存渲染后的 HTML（取舍 6）。
            payload    jsonb NOT NULL DEFAULT '{}'::jsonb,
            -- 已读时间；NULL = 未读（取舍 5）。
            read_at    timestamptz,
            created_at timestamptz NOT NULL DEFAULT now()
        )
        """
    )

    # 幂等唯一真相源。带 user_id 前缀，见取舍 1。
    op.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS ux_notifications_user_dedupe "
        "ON public.notifications USING btree (user_id, dedupe_key)"
    )

    # 列表查询：`WHERE user_id = $1 ORDER BY created_at DESC LIMIT n`。
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_notifications_user_created "
        "ON public.notifications USING btree (user_id, created_at DESC)"
    )

    # 未读角标：`SELECT count(*) WHERE user_id = $1 AND read_at IS NULL`。
    # 用部分索引而不是全量 (user_id) 索引：已读行会随时间无限增长，
    # 把它们排除在索引外能让角标查询的开销只随**未读数**增长。
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_notifications_user_unread "
        "ON public.notifications USING btree (user_id) WHERE read_at IS NULL"
    )

    # ── 用户级渠道偏好（只管邮件，见取舍 3）──
    #
    # 不预置行：缺行即默认开启（取舍 4）。
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS public.user_notification_prefs
        (
            user_id       varchar(36) NOT NULL REFERENCES public.users (id),
            kind          varchar(64) NOT NULL,
            email_enabled boolean     NOT NULL DEFAULT TRUE,
            updated_at    timestamptz NOT NULL DEFAULT now(),
            PRIMARY KEY (user_id, kind)
        )
        """
    )


def downgrade() -> None:
    # 顺序：偏好表无外键指向 notifications，两者互不依赖，先删偏好再删本体即可。
    op.execute("DROP TABLE IF EXISTS public.user_notification_prefs")
    op.execute("DROP TABLE IF EXISTS public.notifications")
