"""账号注销：冻结 → 可撤销 → 到期归档并删除。

**状态机**（`users.account_status`）：
  active          正常
  pending_deletion 已申请注销：资产冻结、除「导出」与「撤销」外的操作一律拒绝
  purged          到期已归档并清除个人数据（终态，不可撤销）

**为什么"清空资产"实现为冻结而不是立即删除**：
产品要求是「注销后不允许其他操作，只允许下载/导出，且 1 周内可随时撤销」。
如果申请时就物理删除资产，撤销将无从恢复 —— 那等于没有撤销窗口。
因此申请时**冻结**（余额移入 `credits_frozen`、API Key 吊销、定时任务停用），
到期再真删。这样三个要求同时成立，且冻结期间用户仍能导出。

**为什么单独存 `credits_frozen` 而不是把 `credits` 清零后另存**：
`credits` 是**当前可用余额**，计费路径会直接读它。注销期间它必须是 0
（否则并发中的回合仍能扣费）。撤销时把 `credits_frozen` 写回 `credits`。
两个字段都是 `integer`，与既有 `users.credits` 同类型，避免精度问题。

**为什么不新建 `account_deletions` 表**：
注销状态是用户的一对一属性（一个用户同时只能有一个注销申请），
放 `users` 上不会有重复行问题；独立表反而要在每条鉴权路径上多一次 join，
而鉴权是热路径。审计轨迹靠 `audit_logs`（action = `account.delete_*`）。

**`deletion_due_at` 由服务端计算并落库**，而不是每次按 `requested_at + 7d`
现算：宽限期是**契约**（写进了对用户的告知），落库后即使将来调整默认窗口，
已申请的用户的到期时间也不会被悄悄改变。

索引：`account_status` 上建部分索引（`WHERE account_status = 'pending_deletion'`）。
到期清理任务要定期扫描"该处理"的账号，而这类账号在生产库中占比极小；
全表扫描 `users` 在十万级租户下会成为负担，部分索引让它保持常数级。

Revision ID: 0019_account_deletion
Revises: 0018_audit_append_only
"""

from typing import Sequence, Union

from alembic import op

revision: str = "0019_account_deletion"
down_revision: Union[str, Sequence[str], None] = "0018_audit_append_only"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # 用 NOT NULL DEFAULT 'active'：存量用户（含所有既有账号）行为逐字不变。
    op.execute(
        "ALTER TABLE public.users ADD COLUMN IF NOT EXISTS account_status "
        "character varying(16) NOT NULL DEFAULT 'active';"
    )
    op.execute(
        "ALTER TABLE public.users ADD COLUMN IF NOT EXISTS deletion_requested_at "
        "timestamp with time zone;"
    )
    op.execute(
        "ALTER TABLE public.users ADD COLUMN IF NOT EXISTS deletion_due_at "
        "timestamp with time zone;"
    )
    # 冻结期间的余额暂存处。可为 NULL（表示从未冻结过）。
    op.execute(
        "ALTER TABLE public.users ADD COLUMN IF NOT EXISTS credits_frozen integer;"
    )
    # 冻结前被我们停用的对象 id 列表（jsonb）。
    #
    # 为什么需要它：撤销时必须**精确恢复**。若无此记录，只能笼统地把
    # `enabled` 全部置 true —— 那会把用户自己早就停用的定时任务也重新打开。
    # 这是"撤销后状态被悄悄改变"的一类隐性问题，用户很难察觉却可能产生真实副作用
    # （定时任务重新开跑、开始扣费）。
    op.execute(
        "ALTER TABLE public.users ADD COLUMN IF NOT EXISTS deletion_freeze_state jsonb;"
    )

    # 部分索引：只有"待删除"的账号需要被到期任务扫描到。
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_users_pending_deletion "
        "ON public.users (deletion_due_at) "
        "WHERE account_status = 'pending_deletion';"
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS public.ix_users_pending_deletion;")
    op.execute("ALTER TABLE public.users DROP COLUMN IF EXISTS deletion_freeze_state;")
    op.execute("ALTER TABLE public.users DROP COLUMN IF EXISTS credits_frozen;")
    op.execute("ALTER TABLE public.users DROP COLUMN IF EXISTS deletion_due_at;")
    op.execute("ALTER TABLE public.users DROP COLUMN IF EXISTS deletion_requested_at;")
    op.execute("ALTER TABLE public.users DROP COLUMN IF EXISTS account_status;")
