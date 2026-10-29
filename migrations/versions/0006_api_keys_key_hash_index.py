"""api_keys.key_hash 部分唯一索引 —— 鉴权已是全表扫描，且缺少「同 hash 只能有一把有效 key」的保证。

背景（已核实）
--------------
`api_keys` 建表时只有 `id` 主键与 `user_id` 外键，**`key_hash` 上没有索引**。
而鉴权链路（`internal/api/middleware.go`）每个 `X-API-Key` 请求都会执行：

    SELECT u.id, u.role, ...
      FROM users u
      JOIN api_keys ak ON ak.user_id = u.id
     WHERE ak.key_hash = $1
       AND COALESCE(ak.revoked, false) = false
       AND (ak.expires_at IS NULL OR ak.expires_at > NOW())

`key_hash` 无索引 ⇒ 这条等值查询走全表扫描。key 数量一多，**每一个带 API Key
的请求**都要扫一遍整张表。这不是"以后再说"的优化：用户级自助上线后 key 数量
会从 0 开始增长，而鉴权在每一次调用上。

为什么是**部分**唯一索引
------------------------
只把未吊销的行纳入唯一性：

  - 轮换 = 吊销旧的 + 创建新的。旧行 `revoked = true` 后仍留在表里（审计需要），
    若做全表唯一，同一 hash 的历史行会与新行冲突 —— 但那本来就不该发生，
    真正要保证的是"**同一时刻不能有两把有效 key 共用 hash**"。
  - 已吊销行不参与唯一性，也让"先吊销、再同 hash 重建"这种运维救济不被挡住。

谓词用 `COALESCE(revoked, false) = false` 而不是 `revoked = false`：建表时
`revoked` 没有 `NOT NULL` 也没有默认值，存量行与手工插入的行都可能是 NULL，
而 NULL 在这个系统里的语义一直是"未吊销"（middleware 就是这么判断的）。
写成 `revoked = false` 会让 NULL 行落不进索引，既失去唯一性也利用不上索引。

存量数据
--------
若已有两行未吊销记录共用同一 `key_hash`，建唯一索引会失败。这里 fail-fast 并
列出冲突，**不静默删数据** —— 那可能是两把正在被使用的 key，删错一把等于线上
服务中断。处理方式与 `0003_system_settings_key_unique` 一致。

Revision ID: 0006_api_keys_key_hash_index
Revises: 0005_user_onboarding
"""

from collections.abc import Sequence

from alembic import context, op

revision: str = "0006_api_keys_key_hash_index"
down_revision: str | Sequence[str] | None = "0005_user_onboarding"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # 离线模式（`alembic upgrade --sql`）只生成 DDL，不连接数据库：
    # 那里没有真实行可查，重名检查跳过（同 0003 的处理）。
    if not context.is_offline_mode():
        duplicates = op.get_bind().exec_driver_sql(
            """
            SELECT key_hash, COUNT(*) AS n
              FROM public.api_keys
             WHERE key_hash IS NOT NULL
               AND COALESCE(revoked, false) = false
             GROUP BY key_hash
            HAVING COUNT(*) > 1
             ORDER BY n DESC
             LIMIT 20
            """
        ).fetchall()

        if duplicates:
            detail = "; ".join(
                "key_hash={}... x{}".format(str(row[0])[:12], row[1]) for row in duplicates
            )
            raise RuntimeError(
                "cannot create unique index ux_api_keys_key_hash_active: existing "
                "duplicate key_hash among active api keys -> {}. Revoke the redundant "
                "ones and re-run `alembic upgrade head`.".format(detail)
            )

    op.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS ux_api_keys_key_hash_active "
        "ON public.api_keys USING btree (key_hash) "
        "WHERE (COALESCE(revoked, false) = false)"
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS public.ux_api_keys_key_hash_active")
