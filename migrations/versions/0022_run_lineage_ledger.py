"""run 级任务线账的控制态表 ``run_lineage_ledger``（M15b，论文 §5.2「子 Agent 续跑未计配额」）

Revision ID: 0022_run_lineage_ledger
Revises: 0021_subagent_lineage_root
Create Date: 2026-10-18

缺口：一条任务线可以被反复续跑（每次派**全新 run_id**），token 只记在自己那一行，所以
"这条线到今天一共花了多少"没有任何上限在管 —— 续跑次数就是免费的额度。0021 有了账本单位
（``root_run_id``），本轮加闸门要读的**控制态**。

**表里只有控制态，没有 ``used``**。用量的唯一真相是 0021 之后按 ``root_run_id`` 对
``subagent_runs`` 的求和（``SubagentRunStore.lineage_usage``）。把累计抄一份到本表就制造了
第二份真相，而它一定会对不上：终态写库与"加累计"是两条语句，中间崩溃、补偿、人工修数据都会
让两边各说各话 —— 预算账上最坏的一类静默。这与 0021"物化根但不物化用量"、M12"不建第二张
近重复表"是同一条取舍。

**``reserved``（在途预留）也不在这张表里**（与本片最初设想不同，理由写在这里，免得下一轮又被
搬回来）：预留是**到点就该消失**的协调态。写进 PG 就得配一个回收任务 —— 崩溃的 run 会把它的
预留永久留在条线上，那条线从此"被占住"、新派发一律被拒，比没有闸门更糟。放 Redis（sorted set，
member=``run_id\\ttokens``、score=到期时刻、读时先按 score 清过期）则由到期自动回收，正常收尾
再显式摘掉；Redis 整个不可用时退回"只结算不预留"，见 ``app/subagent/line_ledger.py``。

``cap_tokens`` 的三态（不用 NULL 是为了把两种意图分开）：

* ``0``  = **跟随默认**（``SUBAGENT_LINE_MAX_TOKENS``），也就是"这条线没被单独设过"；
* ``-1`` = **这条线不限**。运维的显式意图，与"没设过"必须可区分 —— 否则"默认值改了"会
  悄悄把一条被明确放行的大线也关掉；
* ``>0`` = 显式上限，也是 M15c 人工提额之后写入的值。

``paused`` / ``pause_reason`` 是 M15c 的落点：线花到上限 ⇒ 置 paused 并生成待办，闸门见
paused 即停（不再派新段、在途段在下一轮开头收口）；人把 ``cap_tokens`` 提上去或清 paused 后，
由续跑接上。``updated_by`` 记是谁改的 —— 这是提额这件事唯一的审计痕迹。

``tenant_id`` 参与读写过滤：闸门这条查询的形状是"给我一个 run id 我就能问这条线花了多少"，
不按租户收就是跨租户读数（与 0021 的 ``LINEAGE_USAGE_SQL`` 同一条理由）。本表的读同样带
``tenant_id`` 条件：拿别租户的 ``root_run_id`` 来问，读不到它被抬过的上限。

"""

from typing import Sequence, Union

from alembic import op

revision: str = "0022_run_lineage_ledger"
down_revision: Union[str, Sequence[str], None] = "0021_subagent_lineage_root"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS run_lineage_ledger (
            root_run_id  varchar(64) NOT NULL,
            tenant_id    varchar(36),
            cap_tokens   bigint NOT NULL DEFAULT 0,
            paused       boolean NOT NULL DEFAULT false,
            pause_reason varchar(64),
            updated_by   varchar(36),
            created_at   timestamptz NOT NULL DEFAULT now(),
            updated_at   timestamptz NOT NULL DEFAULT now(),
            PRIMARY KEY (root_run_id)
        )
        """
    )
    # M15c 的待办列表要回答"这个租户有哪些线停在半路等人提额" —— 只有被暂停的行需要进这个索引，
    # 用 partial index 而不是 (tenant_id, paused)：绝大多数线永远不会被暂停。
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS ix_run_lineage_ledger_paused
            ON run_lineage_ledger (tenant_id, updated_at)
         WHERE paused
        """
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS ix_run_lineage_ledger_paused")
    op.execute("DROP TABLE IF EXISTS run_lineage_ledger")
