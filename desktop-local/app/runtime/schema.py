"""本地运行时的最小表结构（``CREATE TABLE IF NOT EXISTS``，可反复执行）。

为什么不走 ``alembic``：仓库根的 ``migrations/`` 是**云端 Go 网关**的 schema（租户、计费、
知识库），把它整套搬到桌面的数据目录里没有必要，也会让本地首启多出几十张空表。本地 core
只需要对话 / 消息 / Agent 三张表，因此这里用 DDL 直接建，幂等、无版本历史。

约定：

- 所有表都带 ``tenant_id`` + ``user_id``。本地固定 ``1`` / ``local``（见 ``app/api/auth.py``），
  但列必须存在，否则将来接云端托管时无法无损迁移。
- 时间列一律 ``TIMESTAMPTZ``：本地机器也可能换时区，带时区才不会让"最近会话"排序错乱。
- ``local_messages.seq`` 用 ``BIGSERIAL``：同一毫秒内写入的两条消息也能稳定排序，
  不要依赖 ``created_at`` 的唯一性。
- 表名统一 ``local_`` 前缀：同库里可能还有云端 schema，前缀让 ``DROP`` / 备份时的边界一目了然。
"""

from __future__ import annotations

import asyncpg

#: 本地 schema 的语义版本。目前只有"建表"，还没有需要迁移的变更；
#: 第一次需要 ``ALTER TABLE`` 时，把这里自增并引入按版本执行的语句。
SCHEMA_VERSION = 1

CREATE_SESSIONS = """
CREATE TABLE IF NOT EXISTS local_sessions (
    id TEXT PRIMARY KEY,
    tenant_id BIGINT NOT NULL DEFAULT 1,
    user_id TEXT NOT NULL DEFAULT 'local',
    title TEXT NOT NULL DEFAULT '',
    model TEXT NOT NULL DEFAULT '',
    agent_id TEXT,
    message_count INTEGER NOT NULL DEFAULT 0,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
)
"""

CREATE_MESSAGES = """
CREATE TABLE IF NOT EXISTS local_messages (
    id TEXT PRIMARY KEY,
    seq BIGSERIAL NOT NULL,
    session_id TEXT NOT NULL REFERENCES local_sessions(id) ON DELETE CASCADE,
    tenant_id BIGINT NOT NULL DEFAULT 1,
    user_id TEXT NOT NULL DEFAULT 'local',
    role TEXT NOT NULL,
    content TEXT NOT NULL DEFAULT '',
    model TEXT NOT NULL DEFAULT '',
    agent_id TEXT,
    prompt_tokens INTEGER,
    completion_tokens INTEGER,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT local_messages_role_check CHECK (role IN ('system', 'user', 'assistant'))
)
"""

CREATE_AGENTS = """
CREATE TABLE IF NOT EXISTS local_agents (
    id TEXT PRIMARY KEY,
    tenant_id BIGINT NOT NULL DEFAULT 1,
    user_id TEXT NOT NULL DEFAULT 'local',
    name TEXT NOT NULL,
    description TEXT NOT NULL DEFAULT '',
    system_prompt TEXT NOT NULL DEFAULT '',
    model TEXT NOT NULL DEFAULT '',
    tools JSONB NOT NULL DEFAULT '[]'::jsonb,
    enabled BOOLEAN NOT NULL DEFAULT TRUE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
)
"""

CREATE_SESSION_MAP_WORKSPACES = """
CREATE TABLE IF NOT EXISTS local_session_map_workspaces (
    id TEXT PRIMARY KEY,
    tenant_id BIGINT NOT NULL DEFAULT 1,
    user_id TEXT NOT NULL DEFAULT 'local',
    name TEXT NOT NULL DEFAULT '',
    viewport JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
)
"""

CREATE_SESSION_MAP_NODES = """
CREATE TABLE IF NOT EXISTS local_session_map_nodes (
    id TEXT PRIMARY KEY,
    workspace_id TEXT NOT NULL REFERENCES local_session_map_workspaces(id) ON DELETE CASCADE,
    session_id TEXT,
    parent_node_id TEXT,
    edge_kind TEXT NOT NULL DEFAULT 'manual',
    x INTEGER NOT NULL DEFAULT 0,
    y INTEGER NOT NULL DEFAULT 0,
    title TEXT NOT NULL DEFAULT '',
    color TEXT NOT NULL DEFAULT '',
    collapsed BOOLEAN NOT NULL DEFAULT FALSE,
    hidden BOOLEAN NOT NULL DEFAULT FALSE,
    pinned BOOLEAN NOT NULL DEFAULT FALSE,
    branch_from_seq INTEGER,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
)
"""

CREATE_MESSAGES_INDEX = """
CREATE INDEX IF NOT EXISTS local_messages_session_seq_idx ON local_messages (session_id, seq)
"""

CREATE_SESSIONS_INDEX = """
CREATE INDEX IF NOT EXISTS local_sessions_updated_idx ON local_sessions (updated_at DESC)
"""

CREATE_AGENTS_INDEX = """
CREATE INDEX IF NOT EXISTS local_agents_owner_idx ON local_agents (tenant_id, user_id)
"""

CREATE_SESSION_MAP_NODES_INDEX = """
CREATE INDEX IF NOT EXISTS local_session_map_nodes_ws_idx ON local_session_map_nodes (workspace_id)
"""

#: 建表顺序有意义（``local_messages`` 引用 ``local_sessions``），不要重排。
SCHEMA_STATEMENTS: tuple[str, ...] = (
    CREATE_SESSIONS,
    CREATE_MESSAGES,
    CREATE_AGENTS,
    CREATE_SESSION_MAP_WORKSPACES,
    CREATE_SESSION_MAP_NODES,
    CREATE_MESSAGES_INDEX,
    CREATE_SESSIONS_INDEX,
    CREATE_AGENTS_INDEX,
    CREATE_SESSION_MAP_NODES_INDEX,
)

#: 本模块负责的表（测试与诊断用）。
TABLES: tuple[str, ...] = (
    "local_sessions",
    "local_messages",
    "local_agents",
    "local_session_map_workspaces",
    "local_session_map_nodes",
)

#: 建表用的连接超时（秒）。本地 loopback，超过这个数就是真出问题了。
SCHEMA_TIMEOUT_SECONDS = 10.0


async def ensure_schema(dsn: str, *, timeout: float = SCHEMA_TIMEOUT_SECONDS) -> None:
    """连上 ``dsn`` 并逐条执行建表语句。

    **不吞异常**：连不上 / 权限不足要让调用方看到真实原因（``store.LocalStore`` 会把它
    包成 ``StorageUnavailableError`` 交给 API 层转 503）。逐条执行而不是拼成一个大字符串，
    是因为 asyncpg 的 ``execute`` 只在不带参数时才接受多语句，且失败时逐条更容易定位。
    """
    conn = await asyncpg.connect(dsn=dsn, timeout=timeout)
    try:
        for statement in SCHEMA_STATEMENTS:
            await conn.execute(statement)
    finally:
        await conn.close()
