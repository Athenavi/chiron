"""cron_jobs 按租户列出/触发的索引（多租户隔离落地后的查询形状）

背景（已核实）
--------------
`cron_jobs` 建表时只有 `id` 主键（`0001_authoritative_baseline.py:912`），
**`tenant_id` 上没有任何索引**。而定时任务的所有租户级查询都是同一个形状：

    SELECT ... FROM cron_jobs WHERE tenant_id = $1 ORDER BY created_at DESC

同时调度器每个周期还要 `WHERE enabled = true` 全量扫描一次（`cron_scheduler.go`
的 `sync()`），那是**全平台**的查询，与租户无关，本次不动。

为什么现在加
------------
隔离谓词本身是本次修复加的（此前列表/创建/更新/删除/触发都没有租户谓词），
没有索引时 `tenant_id = $1` 会退化成全表扫描，且 `ORDER BY created_at DESC`
还要额外排序。行数少时无感，但 cron 表是每租户只增不减的配置表，租户数一多
就变成"每次打开定时任务页面扫全表"。

设计取舍
--------
1. **复合索引 `(tenant_id, created_at DESC)`**，而不是单列 `tenant_id`：
   查询的谓词与排序都覆盖，可同时消除扫描与排序。前缀 `tenant_id` 也能单独
   服务于"按租户统计/清理"这类只带谓词的查询。
2. **不加 `WHERE enabled` 部分索引**：调度器的 `enabled = true` 查询不带租户，
   用不上本索引；给它另建索引会让写放大而收益存疑（该表是小配置表）。
3. **不在事务外建 CONCURRENTLY**：迁移由 Alembic 在发布窗口执行，本表行数与
   写入频率都极低（运维手工维护的定时任务），普通 `CREATE INDEX` 的短暂锁可接受；
   若将来行数已大，应改为单独的非事务迁移。
4. `IF NOT EXISTS`：可重入，重复执行不报错。

Revision ID: 0013_cron_jobs_tenant_index
Revises: 0012_notifications_retention
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0013_cron_jobs_tenant_index"
down_revision: str | Sequence[str] | None = "0012_notifications_retention"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_cron_jobs_tenant_created "
        "ON public.cron_jobs USING btree (tenant_id, created_at DESC)"
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS public.idx_cron_jobs_tenant_created")
