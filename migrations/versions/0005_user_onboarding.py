"""新用户引导状态表 user_onboarding —— 记录每一步的完成/跳过与时间点。

背景
----
注册后到「发出第一轮对话」之间，用户此前只能自己摸索：没有引导、没有完成标记，
也无法回答「有多少新账号走完了引导」。这张表就是把这件事变成可查询的事实。

为什么一行一步（(user_id, step) 主键）而不是一行一用户
----------------------------------------------------
产品上要的不是「他走完了没有」这一个布尔值，而是**每一步的完成率与流失点**：
如果 80% 的人停在「选默认模型」，问题就在那一步而不是整条流程。一行一用户只能
回答前者，回答后者要么加三列（每加深一步就改表），要么塞 JSON（无法按步骤聚合）。
一行一步让 `GROUP BY step` 直接给出漏斗。

「整个流程是否已结束」不再单独存一列 —— 它由步骤状态推导：全部步骤都有
`completed_at` 或 `skipped_at` 即视为结束。多存一份冗余状态只会带来两者不一致。

为什么不在注册事务里写
----------------------
建表只是为了让「引导只对走新注册流程的用户展示」。存量用户（本迁移之前注册的）
在该表里**没有行**，因此不会被弹一次与身份不符的引导 —— 这正是 roadmap 里
「不打断已有用户」的要求。注册时写入放在事务 commit **之后**（失败只告警），
理由与 `usage_events` 相同：引导状态的写入绝不能让一个已经合法的账号注册回滚。

Revision ID: 0005_user_onboarding
Revises: 0004_usage_events
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0005_user_onboarding"
down_revision: str | Sequence[str] | None = "0004_usage_events"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS public.user_onboarding
        (
            user_id      varchar(36) NOT NULL
                REFERENCES public.users (id) ON DELETE CASCADE,
            step         varchar(32) NOT NULL,
            completed_at timestamptz,
            skipped_at   timestamptz,
            created_at   timestamptz NOT NULL DEFAULT now(),
            updated_at   timestamptz NOT NULL DEFAULT now(),
            PRIMARY KEY (user_id, step)
        )
        """
    )
    # 统计用：按步骤聚合完成率/跳过率。主键的 (user_id, step) 前缀帮不上
    # "按 step 分组" 这种全局聚合，因此单独建一条。
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_user_onboarding_step "
        "ON public.user_onboarding USING btree (step)"
    )


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS public.user_onboarding")
