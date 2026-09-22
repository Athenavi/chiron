"""session fork 关系 + 会话地图布局表

Revision ID: d2f8a1c93e07
Revises: c1a7d3f92b04
Create Date: 2026-09-22

背景
----
两件事一起做，因为它们是同一件事的两面：

1. **会话分支（fork）**：目标设计参照 `vendor/dsh-synapse`（DeepSeek Harness 的
   可视化对话工作台插件）—— 它按 DSH 原生 `session.header.parentSession` 画连线，
   `seedLength` 记录 durable fork 切点。我们的 `sessions` 表此前**没有任何父子关系**，
   所以地图即使画出来也只有孤立方块。
2. **地图布局落库**：布局此前只在浏览器 `localStorage['chiron.sessionMap.v2']`，
   换设备/清缓存即丢，多实例下也无法共享。

存储分层（与运行期缓存一致）：**Redis 为热层**（读写快，带 TTL），
**PostgreSQL 为权威层**（异步落库）。本迁移只建权威层。

幂等性
------
全部 `IF NOT EXISTS`；列已存在时重复执行不报错（与既有库用 init.sql 建表 +
`alembic stamp` 对齐的部署方式兼容）。
"""
from typing import Sequence, Union

from alembic import op

# revision identifiers, used by Alembic.
revision: str = 'd2f8a1c93e07'
down_revision: Union[str, Sequence[str], None] = 'c1a7d3f92b04'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # ── 1. 会话分支：父子关系 + 分叉点 ──
    # parent_session_id 自引用；父会话被删时子会话保留（SET NULL），
    # 因为子会话本身是独立会话（分叉后的内容不依赖父会话继续存在）。
    op.execute(
        "ALTER TABLE sessions ADD COLUMN IF NOT EXISTS parent_session_id VARCHAR(36) "
        "REFERENCES sessions(id) ON DELETE SET NULL"
    )
    # branch_from_seq：分叉点之前（含）从父会话复制过来的消息条数。
    # 它是"这条分支从对话的哪一步长出来"的唯一依据 —— 地图连线与详情面板都读它。
    op.execute("ALTER TABLE sessions ADD COLUMN IF NOT EXISTS branch_from_seq INTEGER")
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_sessions_parent_session_id ON sessions (parent_session_id)"
    )

    # ── 2. 地图工作区（画布）──
    # 一个用户可以有多个画布；viewport 存相机（scale/tx/ty），与 dsh-synapse 的
    # canvas 状态对应。nodes 不放这里，避免大 JSON 反复重写。
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS session_map_workspaces (
            id          varchar(64)  NOT NULL PRIMARY KEY,
            tenant_id   varchar(36),
            user_id     varchar(36),
            name        varchar(255) NOT NULL DEFAULT '',
            viewport    jsonb        NOT NULL DEFAULT '{}'::jsonb,
            created_at  timestamp    NOT NULL DEFAULT now(),
            updated_at  timestamp    NOT NULL DEFAULT now()
        )
        """
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS session_map_workspaces_owner_idx "
        "ON session_map_workspaces (tenant_id, user_id)"
    )

    # ── 3. 地图节点 ──
    # 一个节点 = 「对某个会话的引用」+ 布局（坐标/颜色/折叠/隐藏）。
    # 关键字段是 parent_node_id + edge_kind：地图上的连线由此而来。
    #   edge_kind='branch'    → 会话 fork 关系（由 sessions.parent_session_id 推导）
    #   edge_kind='reference' → @ 提及 / 子 Agent run 的父子等派生关系
    #   edge_kind='manual'    → 用户手动连线
    # session_id 可空：便签节点没有对应会话（与既有语义一致）。
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS session_map_nodes (
            id              varchar(64)  NOT NULL PRIMARY KEY,
            workspace_id    varchar(64)  NOT NULL
                            REFERENCES session_map_workspaces(id) ON DELETE CASCADE,
            session_id      varchar(36)  REFERENCES sessions(id) ON DELETE CASCADE,
            parent_node_id  varchar(64)  REFERENCES session_map_nodes(id) ON DELETE SET NULL,
            edge_kind       varchar(16)  NOT NULL DEFAULT 'manual',
            x               integer      NOT NULL DEFAULT 0,
            y               integer      NOT NULL DEFAULT 0,
            title           varchar(255),
            color           varchar(16),
            collapsed       boolean      NOT NULL DEFAULT false,
            hidden          boolean      NOT NULL DEFAULT false,
            pinned          boolean      NOT NULL DEFAULT false,
            branch_from_seq integer,
            created_at      timestamp    NOT NULL DEFAULT now(),
            updated_at      timestamp    NOT NULL DEFAULT now()
        )
        """
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS session_map_nodes_workspace_idx "
        "ON session_map_nodes (workspace_id)"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS session_map_nodes_session_idx "
        "ON session_map_nodes (session_id)"
    )


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS session_map_nodes")
    op.execute("DROP TABLE IF EXISTS session_map_workspaces")
    op.execute("DROP INDEX IF EXISTS ix_sessions_parent_session_id")
    op.execute("ALTER TABLE sessions DROP COLUMN IF EXISTS branch_from_seq")
    op.execute("ALTER TABLE sessions DROP COLUMN IF EXISTS parent_session_id")
