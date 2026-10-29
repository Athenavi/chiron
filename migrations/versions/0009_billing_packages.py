"""套餐包 billing_packages + 用户权益 user_entitlements —— 一次性套餐（P2-2）。

背景（已核实）
--------------
付费侧此前只有纯预付费 credits：`payments` 记录
`(id, user_id, channel, credits, amount_cents, currency, status, qr_code,
provider_order_id, trade_no, created_at, paid_at, expired_at)`，`ConfirmPayment`
推进 `pending→paid` 后调 `AddCredits(reason="<channel>_topup")` 一次性入账。
全仓**没有任何套餐/权益实现**（`rg 'entitlement|package_code|plan_code|套餐'`
排除 `.worktrees`/`vendor`/`docs` 后仅命中前端两个构建脚本与 `zh-CN/errors.ts`
的无关文本），因此可以干净地从头设计。

而所有既有的「权益类」管控都挂在 **tenant_id** 上：
`ent_quota_pools(tenant_id, resource_type, total_amount, period)`、
`ent_model_policies(tenant_id, role_id, allowed_models, per_model_limits)`、
并发上限取自 `ent_quota_pools.concurrency`。

设计取舍
--------
1. **套餐卖给用户（user_id），不卖给租户**。团队租户里一个成员买套餐不该把权益
   给整个租户，否则任何人都能通过买最低档把全租户升级。因此本表按 `user_id`
   而**不是** `tenant_id` —— 这是与既有管控唯一的、也是必要的分层差异。
2. **目录与权益分两张表**。`billing_packages` 是后台可 CRUD 的目录；
   `user_entitlements` 是"某人已获得什么"的**快照**（`allowed_models` 直接拷贝，
   不 JOIN 目录现读）。已售出的权益不能因为运营改价/改权益而变动 —— 这与
   `0007_invoice_requests` 对金额做快照是同一条理由。
3. **一次性、不引入 `billing_cycle`**（2026-10-28 决策，roadmap §4 P2-2）。
   本表只有 `starts_at` / `expires_at` 两个时间点，没有周期字段、没有续期状态、
   没有定时任务。到期只是 `expires_at < now()`，权益自然失效。
4. **有效期从支付成功起算，不是自然月**。这与 `ent_quota_pools.period`
   （`daily`/`monthly` 自然周期）**刻意不同**：套餐是"买的 30 天"，
   不是"这个月的额度"。混用会让跨越月初的套餐凭空多出或少掉天数。
5. **幂等键是 `payment_id` 唯一索引**：一笔支付只能发一次权益。与
   `ConfirmPayment` 既有幂等风格一致（重复回调/轮询只入账一次），判定靠
   `23505` 而不是"先查后插"——后者在并发下两个请求都能查到"没有"。
6. **不存并发字段**。现有并发闸门只有租户级
   （`TenantResourceManager` + `ent_quota_pools.concurrency`），本期不做用户级
   并发。存一个恒不生效的字段是最坏形态：看代码的人会以为它有用。
7. **`payments` 加 `package_code`**（可空）。回调时需要知道"这单买的是哪个套餐"
   才能发对应权益；不加就得靠核对金额反查，金额可被运营改价而变化，不可靠。
   可空是因为存量订单都是纯充值，没有套餐语义。

判定口径（与租户策略的关系）
----------------------------
用户级权益与租户级策略**取更严者**：模型范围取交集，即"租户允许 ∩ 套餐包含"。
理由：付费不该突破租户管理员的管控 —— 租户只允许模型 A 时，套餐含 A/B 也只能用
A，否则普通成员可以靠买套餐绕过白名单。

状态机
------
`user_entitlements` 无显式 status：生效与否**只由时间决定**
（`starts_at <= now() < expires_at`）。不存冗余状态列，避免"状态是 active 但
时间已过期"这类两个真相不一致的经典问题。退款/撤销（本期不做）将来可用
`revoked_at timestamptz` 做加列，不改变现有语义。

Revision ID: 0009_billing_packages
Revises: 0008_tenant_members
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0009_billing_packages"
down_revision: str | Sequence[str] | None = "0008_tenant_members"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # ── 套餐目录（后台可 CRUD）──
    #
    # `code` 是运营与代码沟通用的稳定标识（如 "starter"/"pro"），`payments.package_code`
    # 存的就是它 —— 存 code 而不是 id，是为了历史订单在套餐被删除后仍可读。
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS public.billing_packages
        (
            id            varchar(36) PRIMARY KEY,
            code          varchar(64) NOT NULL,
            name          varchar(128) NOT NULL,
            -- 一次性发放的 credits（入 credit_transactions）。
            credits       integer NOT NULL,
            price_cents   bigint  NOT NULL,
            currency      varchar(8) NOT NULL DEFAULT 'CNY',
            -- 权益有效天数；> 0 才生效，从支付成功那刻起算（见文件头取舍 4）。
            duration_days integer NOT NULL DEFAULT 30,
            -- 权益快照：本套餐允许的模型清单。空数组 = 不限制模型。
            allowed_models text[] NOT NULL DEFAULT ARRAY[]::text[],
            enabled       boolean NOT NULL DEFAULT TRUE,
            sort_order    integer NOT NULL DEFAULT 0,
            created_at    timestamptz NOT NULL DEFAULT now(),
            updated_at    timestamptz NOT NULL DEFAULT now(),
            CONSTRAINT billing_packages_credits_check  CHECK (credits >= 0),
            CONSTRAINT billing_packages_price_check    CHECK (price_cents >= 0),
            CONSTRAINT billing_packages_duration_check CHECK (duration_days > 0)
        )
        """
    )
    # 目录的唯一真相：code 全站唯一（不按租户，套餐是平台级商品）。
    op.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS ux_billing_packages_code "
        "ON public.billing_packages USING btree (code)"
    )

    # ── 用户权益（快照）──
    #
    # 不建外键到 billing_packages：套餐下线/删除后，已售出的权益必须继续可读。
    # 存在性由发放入口（支付回调）保证，外键并不能多拦住什么，却会挡住
    # "清理下架套餐"这类运维动作（与 0007 不对 payments 建外键同一条理由）。
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS public.user_entitlements
        (
            id             varchar(36) PRIMARY KEY,
            user_id        varchar(36) NOT NULL,
            -- 快照：套餐标识与名称，套餐改动/删除后仍可展示。
            package_code   varchar(64) NOT NULL,
            package_name   varchar(128) NOT NULL,
            -- 快照：本笔权益允许的模型（空数组 = 不限制）。
            allowed_models text[] NOT NULL DEFAULT ARRAY[]::text[],
            -- 来源支付单：既是审计线索，也是幂等键（见下方唯一索引）。
            payment_id     varchar(64) NOT NULL,
            starts_at      timestamptz NOT NULL DEFAULT now(),
            expires_at     timestamptz NOT NULL,
            created_at     timestamptz NOT NULL DEFAULT now(),
            CONSTRAINT user_entitlements_window_check CHECK (expires_at > starts_at)
        )
        """
    )
    # 幂等唯一真相源：一笔支付只发一次权益。重复回调靠 23505 拦截。
    op.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS ux_user_entitlements_payment "
        "ON public.user_entitlements USING btree (payment_id)"
    )
    # 判定路径：`WHERE user_id = $1 AND expires_at > now()`。
    # 带 user_id 前缀，让单个用户的权益查询只扫自己的行。
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_user_entitlements_user_expiry "
        "ON public.user_entitlements USING btree (user_id, expires_at DESC)"
    )

    # ── payments 加 package_code ──
    #
    # 可空：存量订单都是纯充值，没有套餐语义。不加这一列，回调时只能靠核对金额
    # 反查套餐，而金额会被运营改价，不可靠。
    op.execute(
        "ALTER TABLE public.payments "
        "ADD COLUMN IF NOT EXISTS package_code varchar(64)"
    )


def downgrade() -> None:
    # 先撤 payments 的列，再删依赖它的两张表（顺序反了不会报错，但先列后表
    # 更符合"回退到上一版 schema"的直觉）。
    op.execute("ALTER TABLE public.payments DROP COLUMN IF EXISTS package_code")
    op.execute("DROP TABLE IF EXISTS public.user_entitlements")
    op.execute("DROP TABLE IF EXISTS public.billing_packages")
