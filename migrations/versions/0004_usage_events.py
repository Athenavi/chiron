"""用量明细表 usage_events —— 「钱花在哪个模型上」的唯一事实来源。

背景
----
现有三张计费相关表**都没有模型维度**：

  - `credit_transactions`：id / user_id / amount / balance / reason / created_at / turn_id
  - `billing_records`：tenant_id / user_id / session_id / input_tokens / output_tokens /
    cost_cents / group_id / turn_id
  - `run_usage_settlements`：run_id / tenant_id / user_id / session_id / settled_input /
    settled_output / settled_cached / status / ...

于是 `/v1/billing/usage` 只能按天聚合，用户看不到「上周哪个模型花了多少」。这不是
查询写法的限制，而是数据没被记下来。

为什么新建表而不是给 `billing_records` 加列
------------------------------------------
用户已明确选择「独立用量明细表」。除了决策本身，这里也有独立的技术理由：

  1. `billing_records` 被 M19 的 run 对账路径（`run_usage_reconciler.go`）按**回合**写入，
     并靠 `ON CONFLICT (turn_id) DO NOTHING` 做幂等；给它加列会把这个幂等语义与新维度
     耦合在一起，任何一次重放都可能静默丢掉模型信息。
  2. 明细表的粒度更细：一次回合可能调用多个模型（主模型 + 子 agent + 记忆摘要），
     `billing_records` 的一行一回合结构表达不了，硬塞会把成本摊成平均值。
  3. 不加列 ⇒ 不碰现有代价路径的写入契约，回滚只需 DROP TABLE。

与现有表的关系
--------------
本表**只记录，不参与扣费**。credits 的扣减仍由 `billing.Manager.DeductTokens` 负责，
两条路径互不阻塞：明细写入失败只告警，绝不能让已完成的扣费回滚。

`turn_id` 不加唯一约束：同一回合可能有多行（见上面第 2 点）。需要的幂等由写入方
按 `(turn_id, model)` 自行保证。

存量数据
--------
不回填。历史回合没记过模型名，编造一个「unknown」会让统计看起来有数据却不可用；
查询侧把 `model IS NULL` 显示为「未知」即可。

Revision ID: 0004_usage_events
Revises: 0003_system_settings_key_unique
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0004_usage_events"
down_revision: str | Sequence[str] | None = "0003_system_settings_key_unique"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS public.usage_events
        (
            id             bigserial PRIMARY KEY,
            tenant_id      smallint NOT NULL,
            user_id        varchar(36),
            session_id     varchar(128),
            turn_id        varchar(36),
            model          varchar(128),
            provider       varchar(64),
            input_tokens   bigint NOT NULL DEFAULT 0,
            output_tokens  bigint NOT NULL DEFAULT 0,
            cached_tokens  bigint NOT NULL DEFAULT 0,
            cost_cents     integer,
            created_at     timestamptz NOT NULL DEFAULT now()
        )
        """
    )
    # 按用户 + 时间查用量是唯一的高频读法；日期范围 + 用户过滤走这条索引。
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_usage_events_user_time "
        "ON public.usage_events USING btree (user_id, created_at DESC)"
    )
    # 按模型分组统计时，DB 仍需先按用户与时间收窄，所以把 model 放在第二列便于
    # 同一用户同一时间段内做 index-only 聚合。
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_usage_events_user_model_time "
        "ON public.usage_events USING btree (user_id, model, created_at DESC)"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_usage_events_session "
        "ON public.usage_events USING btree (session_id)"
    )


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS public.usage_events")
