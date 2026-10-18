"""subagent_runs 增血缘根列 root_run_id（M15a，run 级预算账的账本单位）

Revision ID: 0021_subagent_lineage_root
Revises: 0020_memory_summary_embedding_space
Create Date: 2026-10-18

背景（`docs/core-mechanisms-paper.md` §5.2「子 Agent 续跑未计配额」）：一条任务线可以被反复
续跑（`resume_subagent` 每次派一个**全新 run_id**），而每条 run 的 token 只记在自己那一行上，
**没有任何口径能把"这条线一共花了多少"问出来**。要设上限就得先有"这条线"的标识。

为什么必须新增列，而不能由现有列推出来（三个都已核实的坑）：

* ``parent_run_id`` **历史上恒为 NULL**：``SubAgentRunner.run()`` 只算过一次
  ``self._parent_run_id or _context_run_id()``，而它算在 ``start_run(...)`` **之后**、只喂给
  事件与运行期缓存（同一段里 ``turn_id``/``depth`` 都传了）⇒ 任务树从未进过库。
* ``turn_id`` **历史上恒为 NULL**：工具层读 ``get_tool_context("turn_id")``，但引擎入口从没设
  过它，Go 的请求体里也没有这个字段（``AgentTask`` 无 ``turn_id``）。
* ``root_session_id`` **跨层不同值**：子任务会话被写成 ``sub:{父会话}:{run_id}``，孙代这列存
  的是那条合成 id ⇒ 按它分组只能收一层。

所以本轮：① 引擎侧把 ``parent_run_id`` 提到落库之前算并真的写进去（见
``app/agent/subagent_runner.py``），② 本列把"根"物化成一行可读的值。

**物化而不是每次 ``WITH RECURSIVE``**：闸门在每次 LLM 调用前都要问一次"这条线花了多少"，
递归 CTE 等于把 O(层数) 的遍历放到最热的路径上；而血缘是**写入即确定、之后不变**的，正好适合
物化。回填这次确实用了一条递归 CTE —— 它只跑一次，且用 ``UNION``（去重）而不是
``UNION ALL``：万一历史里有 ``resumed_from`` 成环，环上的行不是种子、也连不进来，结果是它们
保持 NULL（"未知"），而不是让迁移转圈。

**优先续跑血缘，其次父子**（``resumed_from`` > ``parent_run_id``）：续跑派生的新 run 同时也是
当前回合的一棵新树，若按父取根，一条线的花费会被它自己的每次续跑切成若干段、每段各自有额度 ——
那正是本轮要修的缺陷。

``rerun_of`` **不参与血缘**：它是"按原任务再派一次"（``rerun_subagent``），与"带着已记录的步骤
继续"（``resumed_from``）是两种产品语义 —— 前者是用户主动重复劳动。把它们并成一条线需要单独的
口径决定，不在这里顺手改。

**回填的诚实边界**：历史行的 ``parent_run_id`` 全空，树结构**不可恢复**，所以只能沿
``resumed_from`` 这条确实写进过库的链路归并；其余行按"自己是一条线的根"处理。这会对**旧的嵌套
树少算**（同一棵树里的子/孙各成一线 ⇒ 各自有额度），但不会**跨线多算**（绝不把两条无关的线并成
一条）。新的行由引擎在写入时解析，形状是准的。``resumed_from`` 指向已被删的行时保持 NULL。

``ADD COLUMN`` + 索引都带 ``IF NOT EXISTS``，引擎侧的写入也是单独一条 UPDATE（与
0004/0006/0008 同一惯例）：未执行本迁移的库只丢血缘，不会让普通派发的 ``INSERT`` 因未知列
整条失败。
"""

from typing import Sequence, Union

from alembic import op

revision: str = "0021_subagent_lineage_root"
down_revision: Union[str, Sequence[str], None] = "0020_memory_summary_embedding_space"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute(
        """
        ALTER TABLE subagent_runs
            ADD COLUMN IF NOT EXISTS root_run_id character varying(64)
        """
    )
    # 唯一的读路径是 `WHERE root_run_id = $1 AND tenant_id = $2` 的求和，而它在每次 LLM 调用
    # 前的闸门附近 —— 没索引就是全表扫。
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS ix_subagent_runs_root_run_id
            ON subagent_runs (root_run_id)
        """
    )
    op.execute(
        """
        WITH RECURSIVE chain AS (
            SELECT id, id AS root
              FROM subagent_runs
             WHERE resumed_from IS NULL
            UNION
            SELECT r.id, c.root
              FROM chain c
              JOIN subagent_runs r ON r.resumed_from = c.id
        )
        UPDATE subagent_runs s
           SET root_run_id = c.root
          FROM chain c
         WHERE s.id = c.id
           AND s.root_run_id IS NULL
        """
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS ix_subagent_runs_root_run_id")
    op.execute("ALTER TABLE subagent_runs DROP COLUMN IF EXISTS root_run_id")
