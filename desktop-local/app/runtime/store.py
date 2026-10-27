"""本地运行时的数据访问层（PostgreSQL / asyncpg）。

连接策略：**每次操作自己 ``asyncpg.connect`` + ``close``**。桌面端是单用户 loopback，
并发是个位数，而连接池需要生命周期钩子（我们只能在 ``lifespan`` 里做，且 PG 起不来时
不能阻断进程）—— 为了省几毫秒引入池的停机与泄漏风险不值。真实瓶颈在推理，不在这里。

形状约定：所有方法返回 **纯 dict**（``created_at`` / ``updated_at`` 已转成 ISO 字符串，
``tools`` 已从 JSONB 解成 list）。这样 API 层与测试里的假实现共用同一份形状，
不需要在两处各写一遍序列化。
"""

from __future__ import annotations

import asyncio
import json
import uuid
from collections.abc import Sequence
from datetime import datetime
from typing import Any

import asyncpg

from app.runtime.schema import ensure_schema

#: 本地单租户（与 ``app/api/auth.py::LOCAL_USER`` 对齐）
DEFAULT_TENANT_ID = 1
DEFAULT_USER_ID = "local"

#: ``update_agent`` 允许改的列（**白名单**：字段名直接拼进 SQL，必须来自这里）
AGENT_UPDATABLE_COLUMNS: tuple[str, ...] = (
    "name",
    "description",
    "system_prompt",
    "model",
    "tools",
    "enabled",
)

_MESSAGE_SELECT = (
    "id, seq, session_id, tenant_id, user_id, role, content, model, agent_id,"
    " prompt_tokens, completion_tokens, created_at"
)
_SESSION_SELECT = "id, tenant_id, user_id, title, model, agent_id, message_count, created_at, updated_at"
_AGENT_SELECT = (
    "id, tenant_id, user_id, name, description, system_prompt, model, tools, enabled, created_at, updated_at"
)

CONNECT_TIMEOUT_SECONDS = 10.0


class StorageUnavailableError(RuntimeError):
    """本地 PostgreSQL 不可用（未启动 / DSN 为空 / 建表失败）。

    API 层把它翻成 503：这是"本地模式暂时用不了"，不是请求写错了。
    """


def new_id(prefix: str) -> str:
    """生成本地主键：``<prefix>_<32 位 hex>``。"""
    return f"{prefix}_{uuid.uuid4().hex}"


def _row_to_dict(row: asyncpg.Record) -> dict[str, Any]:
    """``Record`` → JSON 友好的 dict（时间转 ISO 8601）。"""
    data = dict(row)
    for key in ("created_at", "updated_at"):
        value = data.get(key)
        if isinstance(value, datetime):
            data[key] = value.isoformat()
    return data


def _agent_row(row: asyncpg.Record) -> dict[str, Any]:
    """Agent 行额外把 JSONB 的 ``tools`` 解成 list（asyncpg 默认给的是字符串）。"""
    data = _row_to_dict(row)
    tools = data.get("tools")
    if isinstance(tools, str):
        try:
            parsed = json.loads(tools)
        except json.JSONDecodeError:
            parsed = []
        data["tools"] = parsed if isinstance(parsed, list) else []
    elif tools is None:
        data["tools"] = []
    return data


class LocalStore:
    """本地 PG 的最小数据访问层：会话 / 消息 / Agent。"""

    def __init__(
        self,
        dsn: str,
        *,
        tenant_id: int = DEFAULT_TENANT_ID,
        user_id: str = DEFAULT_USER_ID,
    ) -> None:
        self._dsn = dsn
        self.tenant_id = tenant_id
        self.user_id = user_id
        self._schema_ready = False
        self._schema_lock = asyncio.Lock()

    # ------------------------------------------------------------------ 基础
    @property
    def dsn(self) -> str:
        return self._dsn

    async def _connect(self) -> asyncpg.Connection:
        try:
            return await asyncpg.connect(dsn=self._dsn, timeout=CONNECT_TIMEOUT_SECONDS)
        except (OSError, TimeoutError, asyncpg.PostgresError) as exc:
            raise StorageUnavailableError(
                f"连不上本地 PostgreSQL（{type(exc).__name__}: {exc}）；检查 /readyz 的 pg 字段"
            ) from exc

    async def ensure_schema(self) -> None:
        """建表（幂等，单飞：并发请求不会重复跑 DDL）。"""
        if self._schema_ready:
            return
        async with self._schema_lock:
            if self._schema_ready:
                return
            try:
                await ensure_schema(self._dsn)
            except StorageUnavailableError:
                raise
            except (OSError, TimeoutError, asyncpg.PostgresError) as exc:
                raise StorageUnavailableError(f"本地 PostgreSQL 建表失败（{type(exc).__name__}: {exc}）") from exc
            self._schema_ready = True

    # ------------------------------------------------------------------ 会话
    async def create_session(
        self,
        *,
        session_id: str | None = None,
        title: str = "",
        model: str = "",
        agent_id: str | None = None,
    ) -> dict[str, Any]:
        new_session_id = session_id or new_id("sess")
        conn = await self._connect()
        try:
            row = await conn.fetchrow(
                f"""
                INSERT INTO local_sessions (id, tenant_id, user_id, title, model, agent_id)
                VALUES ($1, $2, $3, $4, $5, $6)
                RETURNING {_SESSION_SELECT}
                """,
                new_session_id,
                self.tenant_id,
                self.user_id,
                title,
                model,
                agent_id,
            )
        finally:
            await conn.close()
        return _row_to_dict(row)

    async def get_session(self, session_id: str) -> dict[str, Any] | None:
        conn = await self._connect()
        try:
            row = await conn.fetchrow(
                f"SELECT {_SESSION_SELECT} FROM local_sessions WHERE id = $1 AND tenant_id = $2 AND user_id = $3",
                session_id,
                self.tenant_id,
                self.user_id,
            )
        finally:
            await conn.close()
        return _row_to_dict(row) if row is not None else None

    async def list_sessions(self, *, limit: int = 50, offset: int = 0) -> list[dict[str, Any]]:
        conn = await self._connect()
        try:
            rows = await conn.fetch(
                f"SELECT {_SESSION_SELECT} FROM local_sessions"
                " WHERE tenant_id = $1 AND user_id = $2"
                " ORDER BY updated_at DESC, id DESC LIMIT $3 OFFSET $4",
                self.tenant_id,
                self.user_id,
                limit,
                offset,
            )
        finally:
            await conn.close()
        return [_row_to_dict(row) for row in rows]

    async def touch_session(
        self,
        session_id: str,
        *,
        model: str | None = None,
        agent_id: str | None = None,
        title: str | None = None,
    ) -> None:
        """更新会话的"当前模型 / Agent / 标题"并刷新 ``updated_at``（计数由消息写入自增）。"""
        conn = await self._connect()
        try:
            await conn.execute(
                """
                UPDATE local_sessions
                   SET model = COALESCE($2, model),
                       agent_id = COALESCE($3, agent_id),
                       title = COALESCE($4, title),
                       updated_at = now()
                 WHERE id = $1 AND tenant_id = $5 AND user_id = $6
                """,
                session_id,
                model,
                agent_id,
                title,
                self.tenant_id,
                self.user_id,
            )
        finally:
            await conn.close()

    # ------------------------------------------------------------------ 消息
    async def append_message(
        self,
        *,
        session_id: str,
        role: str,
        content: str,
        model: str = "",
        agent_id: str | None = None,
        prompt_tokens: int | None = None,
        completion_tokens: int | None = None,
        message_id: str | None = None,
    ) -> dict[str, Any]:
        """写一条消息，并在**同一个事务里**把会话的 ``message_count`` / ``updated_at`` 推进。

        同事务很重要：否则"消息写了但计数没动"会让会话列表显示 0 条而点进去有内容。
        """
        new_message_id = message_id or new_id("msg")
        conn = await self._connect()
        try:
            async with conn.transaction():
                row = await conn.fetchrow(
                    f"""
                    INSERT INTO local_messages
                        (id, session_id, tenant_id, user_id, role, content, model, agent_id,
                         prompt_tokens, completion_tokens)
                    VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10)
                    RETURNING {_MESSAGE_SELECT}
                    """,
                    new_message_id,
                    session_id,
                    self.tenant_id,
                    self.user_id,
                    role,
                    content,
                    model,
                    agent_id,
                    prompt_tokens,
                    completion_tokens,
                )
                await conn.execute(
                    "UPDATE local_sessions SET message_count = message_count + 1, updated_at = now()"
                    " WHERE id = $1 AND tenant_id = $2 AND user_id = $3",
                    session_id,
                    self.tenant_id,
                    self.user_id,
                )
        finally:
            await conn.close()
        return _row_to_dict(row)

    async def list_messages(self, *, session_id: str, limit: int = 50) -> list[dict[str, Any]]:
        """按写入顺序（``seq``）返回**最近** ``limit`` 条，再翻回正序。"""
        conn = await self._connect()
        try:
            rows = await conn.fetch(
                f"""
                SELECT {_MESSAGE_SELECT} FROM (
                    SELECT {_MESSAGE_SELECT} FROM local_messages
                     WHERE session_id = $1 AND tenant_id = $2 AND user_id = $3
                     ORDER BY seq DESC
                     LIMIT $4
                ) AS recent
                ORDER BY seq ASC
                """,
                session_id,
                self.tenant_id,
                self.user_id,
                limit,
            )
        finally:
            await conn.close()
        return [_row_to_dict(row) for row in rows]

    # ------------------------------------------------------------------ Agent
    async def create_agent(
        self,
        *,
        name: str,
        description: str = "",
        system_prompt: str = "",
        model: str = "",
        tools: Sequence[str] | None = None,
        enabled: bool = True,
        agent_id: str | None = None,
    ) -> dict[str, Any]:
        new_agent_id = agent_id or new_id("agt")
        conn = await self._connect()
        try:
            row = await conn.fetchrow(
                f"""
                INSERT INTO local_agents
                    (id, tenant_id, user_id, name, description, system_prompt, model, tools, enabled)
                VALUES ($1, $2, $3, $4, $5, $6, $7, $8::jsonb, $9)
                RETURNING {_AGENT_SELECT}
                """,
                new_agent_id,
                self.tenant_id,
                self.user_id,
                name,
                description,
                system_prompt,
                model,
                json.dumps(list(tools or [])),
                enabled,
            )
        finally:
            await conn.close()
        return _agent_row(row)

    async def get_agent(self, agent_id: str) -> dict[str, Any] | None:
        conn = await self._connect()
        try:
            row = await conn.fetchrow(
                f"SELECT {_AGENT_SELECT} FROM local_agents WHERE id = $1 AND tenant_id = $2 AND user_id = $3",
                agent_id,
                self.tenant_id,
                self.user_id,
            )
        finally:
            await conn.close()
        return _agent_row(row) if row is not None else None

    async def list_agents(self, *, limit: int = 100, offset: int = 0) -> list[dict[str, Any]]:
        conn = await self._connect()
        try:
            rows = await conn.fetch(
                f"SELECT {_AGENT_SELECT} FROM local_agents"
                " WHERE tenant_id = $1 AND user_id = $2"
                " ORDER BY created_at ASC, id ASC LIMIT $3 OFFSET $4",
                self.tenant_id,
                self.user_id,
                limit,
                offset,
            )
        finally:
            await conn.close()
        return [_agent_row(row) for row in rows]

    async def update_agent(self, agent_id: str, **fields: Any) -> dict[str, Any] | None:
        """局部更新。只认 :data:`AGENT_UPDATABLE_COLUMNS` 里的列，其余静默忽略。

        没有可改字段时直接回读当前行（不写库、不刷新 ``updated_at``）。
        """
        allowed = {key: value for key, value in fields.items() if key in AGENT_UPDATABLE_COLUMNS}
        if not allowed:
            return await self.get_agent(agent_id)

        columns = sorted(allowed)
        assignments: list[str] = []
        values: list[Any] = []
        for index, column in enumerate(columns, start=2):
            if column == "tools":
                # 显式 ::jsonb：asyncpg 会把 str 当 text，直接赋给 jsonb 列会类型不符
                assignments.append(f"tools = ${index}::jsonb")
                values.append(json.dumps(list(allowed[column] or [])))
            else:
                assignments.append(f"{column} = ${index}")
                values.append(allowed[column])
        conn = await self._connect()
        try:
            row = await conn.fetchrow(
                f"UPDATE local_agents SET {assignments}, updated_at = now()"
                f" WHERE id = $1 AND tenant_id = ${len(columns) + 2} AND user_id = ${len(columns) + 3}"
                f" RETURNING {_AGENT_SELECT}",
                agent_id,
                *values,
                self.tenant_id,
                self.user_id,
            )
        finally:
            await conn.close()
        return _agent_row(row) if row is not None else None

    async def delete_agent(self, agent_id: str) -> bool:
        conn = await self._connect()
        try:
            deleted = await conn.fetchval(
                "DELETE FROM local_agents WHERE id = $1 AND tenant_id = $2 AND user_id = $3 RETURNING id",
                agent_id,
                self.tenant_id,
                self.user_id,
            )
        finally:
            await conn.close()
        return deleted is not None


def store_for(state: Any) -> Any:
    """从 ``app.state`` 取一个 store。

    优先级：``state.local_store``（测试 / 父级接线的注入点，鸭子类型）→ 用
    ``state.pg.dsn`` 构造并在 ``state`` 上缓存 → DSN 为空则抛
    :class:`StorageUnavailableError`（本地库没起来，API 转 503）。

    缓存是按 DSN 判定的：``pg`` 重启后端口会变，DSN 一变就重新构造，不会拿着旧地址硬连。
    """
    injected = getattr(state, "local_store", None)
    if injected is not None:
        return injected

    dsn = str(getattr(getattr(state, "pg", None), "dsn", "") or "").strip()
    if not dsn:
        raise StorageUnavailableError("本地 PostgreSQL 未启动（见 /readyz 的 pg 字段）；对话与 Agent 需要本地库")

    cached = getattr(state, "_local_store", None)
    if isinstance(cached, LocalStore) and cached.dsn == dsn:
        return cached

    store = LocalStore(dsn)
    state._local_store = store
    return store
