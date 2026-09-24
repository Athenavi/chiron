"""混沌工程实验表 ent_chaos_experiments

Revision ID: 0002_ent_chaos_experiments
Revises: 0001_authoritative_baseline
Create Date: 2026-09-24

背景：``internal/api/ent_chaos_handler.go`` 的六个端点长期统一返回 501，其注释自述
「连数据表都不存在 —— ent_chaos_experiments 既未在 shared/models 定义，也未在任何迁移中创建」。
此前的实现会「只写库就返回 created、只改状态就返回 rolled_back」，而故障从未注入；
为避免"假装成功"被整体改成 501。本次补齐真正的实现，第一步就是建表。

列约定与仓库既有企业表一致（varchar(36) 主键 / timestamp 无时区 / jsonb 配置）：

* ``fault_type``：latency / error / timeout / resource（与 app/chaos/engine.py 的 FaultType 对齐）；
* ``target``：作用面标识，取值见应用层白名单（gateway / llm / db / redis / engine …）；
* ``duration_ms`` / ``intensity``：持续时间与强度（0.0~1.0），由引擎的自动回滚使用；
* ``status``：pending / running / completed / failed / rolled_back；
* ``config`` / ``result``：故障特定参数与执行结果（jsonb，读侧 ::text 后 JSON 解析）；
* ``created_by``：发起人（审计用；权限由网关的 ``RequireEntPerm("chaos:manage")`` 把关）。

热路径读取**不走本表**：注入中间件从 Redis 缓存读活跃实验（见 app/chaos/injector.py），
本表是权威存储与审计轨迹。

``IF NOT EXISTS``：与既有迁移风格一致，某些环境若已手工建表则不覆盖。
"""

from typing import Sequence, Union

from alembic import op

revision: str = "0002_ent_chaos_experiments"
down_revision: Union[str, Sequence[str], None] = "0001_authoritative_baseline"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS ent_chaos_experiments (
            id           varchar(36)  NOT NULL PRIMARY KEY,
            tenant_id    varchar(36)  NOT NULL,
            fault_type   varchar(16)  NOT NULL,
            target       varchar(64)  NOT NULL DEFAULT '',
            duration_ms  integer      NOT NULL DEFAULT 1000,
            intensity    real         NOT NULL DEFAULT 0.5,
            status       varchar(16)  NOT NULL DEFAULT 'pending',
            config       jsonb        NOT NULL DEFAULT '{}'::jsonb,
            result       jsonb        NOT NULL DEFAULT '{}'::jsonb,
            created_by   varchar(36)  NOT NULL DEFAULT '',
            started_at   timestamp    NULL,
            completed_at timestamp    NULL,
            created_at   timestamp    NOT NULL DEFAULT now(),
            updated_at   timestamp    NOT NULL DEFAULT now()
        )
        """
    )
    # 列表/状态查询按 (tenant_id, status) 过滤
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_ent_chaos_experiments_tenant_status "
        "ON ent_chaos_experiments (tenant_id, status)"
    )
    # 注入中间件只关心"还在跑"的实验；部分索引让这张表随历史增长时也不拖慢热路径
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_ent_chaos_experiments_active "
        "ON ent_chaos_experiments (status) "
        "WHERE status IN ('pending', 'running')"
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS ix_ent_chaos_experiments_active")
    op.execute("DROP INDEX IF EXISTS ix_ent_chaos_experiments_tenant_status")
    op.execute("DROP TABLE IF EXISTS ent_chaos_experiments")
