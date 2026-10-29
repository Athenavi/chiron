"""默认套餐目录（P2-2c）—— 让套餐包开箱可卖。

背景（已核实）
---------------
`0009_billing_packages` 只建了表，没有种子数据。结果是 P2-2 的购买链路虽然通了，
线上却**一个套餐都卖不了**：`GET /v1/billing/packages` 返回空列表，用户侧只能看到
"管理员还没有上架任何套餐包"，运营则要先手工 `INSERT` 才能验证一次购买。

这条迁移把三个常见档位灌进去，让「部署完就能卖」成立。

为什么是数据迁移而不是代码里的常量
------------------------------------
1. 套餐是**运营数据**而非代码逻辑：价格、credits、有效期都会被改，写死在 Go 里
   意味着改价要发版。
2. `user_entitlements` 对套餐做的是**快照**（`package_code` / `package_name` /
   `allowed_models` 直接拷贝），所以种子数据本身可以在事后随意调整，不会回头
   影响已售出的权益 —— 这正是 0009 文件头取舍 2 的设计目的。
3. 后台管理页（P2-2c 的 CRUD）落地后，这些行就是普通可编辑记录，没有任何特殊地位。

幂等性
------
`billing_packages.code` 上有唯一索引 `ux_billing_packages_code`，因此直接
`ON CONFLICT (code) DO NOTHING` 即可重复执行。**用 ON CONFLICT 而不是
`WHERE NOT EXISTS`**：这里存在可用的唯一约束作冲突目标（不同于 `ent_roles`
那种只有主键的表，见 0008 的注释）。

刻意**不覆盖**已有记录：`DO NOTHING` 意味着运营改过的价格不会被这条迁移
在下次执行时改回去。种子的语义是"缺了才补"，不是"每次重置为默认值"。

档位设计
--------
金额以 **分** 为单位（`price_cents`），1 credit = 1 分（与 `CreatePayment`
的既有换算一致）。三档都留空 `allowed_models`（= 不限制模型）：
- 首次上架就让用户能买到可用的东西，而不是买完发现只能用一个模型；
- 真正需要限模型的场景（如按模型成本差异定价）留给运营在后台按需配置；
- 若种子就写死模型名，`llm_models` 尚未同步的部署会种出**永远用不了**的权益 ——
  正是 P2-2 验收要求里"不能出现用不了但没提示"要避免的情形。

Revision ID: 0010_seed_billing_packages
Revises: 0009_billing_packages
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0010_seed_billing_packages"
down_revision: str | Sequence[str] | None = "0009_billing_packages"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


# (code, name, credits, price_cents, currency, duration_days, sort_order)
#
# 定价口径（1 credit = 1 分，即 100 credits = 1 元）：
#   体验包   1000 credits = ¥10    30 天
#   标准包   6000 credits = ¥60    30 天（隐含 10% 让利，整数便于展示）
#   专业包  20000 credits = ¥180   90 天
# 有效期从支付成功起算（不是自然月），见 0009 文件头取舍 4。
_SEEDS = (
    ("starter", "体验包", 1000, 1000, "CNY", 30, 10),
    ("standard", "标准包", 6000, 6000, "CNY", 30, 20),
    ("pro", "专业包", 20000, 18000, "CNY", 90, 30),
)


def upgrade() -> None:
    # 主键与其它 varchar(36) 表生成方式一致（gen_random_uuid()::text）；
    # billing_packages.id 没有默认值，必须显式给出。
    for code, name, credits, price_cents, currency, duration_days, sort_order in _SEEDS:
        op.execute(
            f"""
            INSERT INTO public.billing_packages
                (id, code, name, credits, price_cents, currency,
                 duration_days, allowed_models, enabled, sort_order,
                 created_at, updated_at)
            VALUES
                (gen_random_uuid()::text, '{code}', '{name}', {credits},
                 {price_cents}, '{currency}', {duration_days},
                 ARRAY[]::text[], TRUE, {sort_order}, now(), now())
            ON CONFLICT (code) DO NOTHING
            """
        )


def downgrade() -> None:
    # 只删本迁移种下的三个 code。
    #
    # **不**删用户在该 code 下已购买的权益：`user_entitlements` 存的是快照
    # （0009 取舍 2），删掉目录行不会、也不该让已售权益消失。
    op.execute(
        "DELETE FROM public.billing_packages "
        "WHERE code IN ('starter', 'standard', 'pro')"
    )
