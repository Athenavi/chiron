"""users(tenant_id, email) 唯一索引 —— 注册邮箱判重的真相源。

背景
----
注册流程原先只靠 `SELECT COUNT(*) FROM users WHERE email = $1 AND tenant_id = $2`
判重，而 users 上 email 没有任何索引：这是**一次全表扫描**，且表上也没有任何唯一
约束，并发注册或任何旁路写入都能造出重复邮箱。

本次改动新增唯一索引 ux_users_tenant_email，作为"邮箱在租户内唯一"的权威保证。
应用侧新增的布隆过滤器（internal/api/user_email_bloom.go）**只做快速否定**以跳过
这次判重，绝不能替代本约束：过滤器同步滞后时靠它兜底。

存量数据
--------
若库中已存在重复的 (tenant_id, email)，建唯一索引必然失败。这里刻意选择
**显式报错并列出冲突行**（fail fast），而不是自动删数据 —— 保留哪个账号属于业务
决策，必须人工处理后再重跑迁移。

注意：tenant_id 为 NULL 的行不参与唯一性判定（PostgreSQL 中 NULL 互不相等），
这与注册流程总是写入具体租户、判重查询也总带 tenant_id 的行为一致。

Revision ID: 0002_users_tenant_email_unique
Revises: 0001_authoritative_baseline
"""

from typing import Sequence, Union

from alembic import op

revision: str = "0002_users_tenant_email_unique"
down_revision: Union[str, Sequence[str], None] = "0001_authoritative_baseline"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    conn = op.get_bind()

    duplicates = conn.exec_driver_sql(
        """
        SELECT tenant_id, email, COUNT(*) AS n
          FROM public.users
         WHERE tenant_id IS NOT NULL
           AND email IS NOT NULL
           AND email <> ''
         GROUP BY tenant_id, email
        HAVING COUNT(*) > 1
         ORDER BY n DESC
         LIMIT 20
        """
    ).fetchall()

    if duplicates:
        detail = "; ".join(
            "tenant={} email={} x{}".format(row[0], row[1], row[2]) for row in duplicates
        )
        raise RuntimeError(
            "cannot create unique index ux_users_tenant_email: existing duplicate "
            "(tenant_id, email) rows found -> {}. Resolve these duplicates "
            "(merge or rename) and re-run `alembic upgrade head`.".format(detail)
        )

    op.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS ux_users_tenant_email "
        "ON public.users USING btree (tenant_id, email)"
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS public.ux_users_tenant_email")
