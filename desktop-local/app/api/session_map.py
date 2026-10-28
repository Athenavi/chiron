"""本地「会话地图」布局 API（对齐云端 Go 网关 ``internal/api/sessionmap.go`` 的形状）。

为什么桌面端必须自己实现：会话地图是 iframe + ``adapter.js`` 缝合层，它的数据面只认
``/v1/session-map/*`` 与 ``/v1/conversations`` —— 这两族端点此前**只存在于 Go 网关**，
本地模式下一律 404，于是地图打开即白屏（真机探针实测：``sessionMap: 404``、
``conversations: 404``）。

与云端实现的取舍差异：

- **不做 Redis 热层**：桌面端是单进程单用户，直写 PG 已经足够快，多一层缓存只会
  多一个「重启后布局丢了」的失败面。
- **不做异步 flusher**：没有多副本竞争，``PUT`` 直接落库即幂等。
- **表前缀** ``local_``：与其余本地表一致（见 ``app/runtime/schema.py``）。

节点只是「对会话的引用 + 布局」，消息本体仍走 ``/v1/conversations/{id}``。
"""

from __future__ import annotations

import json
import uuid
from typing import Any

import asyncpg
from fastapi import APIRouter, HTTPException, Request

from app.runtime.store import DEFAULT_TENANT_ID, DEFAULT_USER_ID

router = APIRouter()

#: 一张画布的节点上限（与云端 ``smMaxNodes`` 对齐，防止异常客户端把库撑爆）
MAX_NODES = 2000

_WS_COLUMNS = "id, name, viewport, created_at, updated_at"
_NODE_COLUMNS = (
    "id, workspace_id, session_id, parent_node_id, edge_kind, x, y, title,"
    " color, collapsed, hidden, pinned, branch_from_seq"
)


def _dsn(request: Request) -> str:
    """取 PG 连接串。

    **来源必须是运行时的 ``app.state.pg``**，不能读 ``settings.postgres_dsn``：
    桌面端用 embedded-pack 起库，监听端口由它自己挑（每次启动都可能不同），
    而 ``settings.postgres_dsn`` 只服务"连外部 PostgreSQL"的场景，单机下恒为空。
    早期这里读的是 settings，结果是本族端点**恒 503** —— 真机探针里会话地图
    打开即白屏。``settings`` 仅作为兜底保留（外部 PG 部署时它才有值）。
    """
    pg = getattr(request.app.state, "pg", None)
    dsn = getattr(pg, "dsn", "") or ""
    if not dsn:
        settings = getattr(request.app.state, "settings", None)
        dsn = getattr(settings, "postgres_dsn", "") or ""
    if not dsn:
        raise HTTPException(status_code=503, detail="本地 PostgreSQL 未配置（/readyz 的 pg 为 false）")
    return dsn


async def _connect(request: Request) -> asyncpg.Connection:
    try:
        return await asyncpg.connect(dsn=_dsn(request), timeout=10.0)
    except (OSError, TimeoutError, asyncpg.PostgresError) as exc:
        raise HTTPException(status_code=503, detail=f"本地 PostgreSQL 不可用（{type(exc).__name__}）") from exc


def _jsonable(value: Any) -> Any:
    """asyncpg 把 JSONB 给成字符串；前端要的是对象。"""
    if isinstance(value, str):
        try:
            return json.loads(value)
        except json.JSONDecodeError:
            return {}
    return value or {}


def _node(row: asyncpg.Record) -> dict[str, Any]:
    data = dict(row)
    return {
        "id": data["id"],
        "session_id": data.get("session_id") or "",
        "parent_node_id": data.get("parent_node_id") or "",
        "edge_kind": data.get("edge_kind") or "manual",
        "x": int(data.get("x") or 0),
        "y": int(data.get("y") or 0),
        "title": data.get("title") or "",
        "color": data.get("color") or "",
        "collapsed": bool(data.get("collapsed")),
        "hidden": bool(data.get("hidden")),
        "pinned": bool(data.get("pinned")),
        "branch_from_seq": data.get("branch_from_seq"),
    }


def _workspace(row: asyncpg.Record, nodes: list[asyncpg.Record]) -> dict[str, Any]:
    return {
        "id": row["id"],
        "name": row["name"] or "",
        "viewport": _jsonable(row["viewport"]),
        "nodes": [_node(n) for n in nodes],
        "created_at": row["created_at"].isoformat() if row["created_at"] else "",
        "updated_at": row["updated_at"].isoformat() if row["updated_at"] else "",
    }


async def _load_nodes(conn: asyncpg.Connection, workspace_id: str) -> list[asyncpg.Record]:
    return list(
        await conn.fetch(
            f"SELECT {_NODE_COLUMNS} FROM local_session_map_nodes WHERE workspace_id = $1"
            " ORDER BY created_at, id",
            workspace_id,
        )
    )


async def _require_workspace(conn: asyncpg.Connection, workspace_id: str) -> asyncpg.Record:
    row = await conn.fetchrow(
        f"SELECT {_WS_COLUMNS} FROM local_session_map_workspaces"
        " WHERE id = $1 AND tenant_id = $2 AND user_id = $3",
        workspace_id,
        DEFAULT_TENANT_ID,
        DEFAULT_USER_ID,
    )
    if row is None:
        raise HTTPException(status_code=404, detail=f"画布不存在：{workspace_id}")
    return row


@router.get("/v1/session-map/workspaces")
async def list_workspaces(request: Request) -> dict[str, Any]:
    conn = await _connect(request)
    try:
        rows = await conn.fetch(
            f"SELECT {_WS_COLUMNS} FROM local_session_map_workspaces"
            " WHERE tenant_id = $1 AND user_id = $2 ORDER BY updated_at DESC LIMIT 50",
            DEFAULT_TENANT_ID,
            DEFAULT_USER_ID,
        )
    finally:
        await conn.close()
    return {
        "data": {
            "workspaces": [
                {
                    "id": r["id"],
                    "name": r["name"] or "",
                    "updated_at": r["updated_at"].isoformat() if r["updated_at"] else "",
                }
                for r in rows
            ]
        }
    }


@router.post("/v1/session-map/workspaces")
async def create_workspace(request: Request) -> dict[str, Any]:
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001 —— 空体/非法体都按「未命名画布」处理
        body = {}
    name = str((body or {}).get("name") or "")
    workspace_id = f"smws_{uuid.uuid4().hex}"
    conn = await _connect(request)
    try:
        row = await conn.fetchrow(
            f"INSERT INTO local_session_map_workspaces (id, tenant_id, user_id, name)"
            f" VALUES ($1, $2, $3, $4) RETURNING {_WS_COLUMNS}",
            workspace_id,
            DEFAULT_TENANT_ID,
            DEFAULT_USER_ID,
            name,
        )
    finally:
        await conn.close()
    return {"data": _workspace(row, [])}


@router.get("/v1/session-map/workspaces/{workspace_id}")
async def get_workspace(request: Request, workspace_id: str) -> dict[str, Any]:
    conn = await _connect(request)
    try:
        row = await _require_workspace(conn, workspace_id)
        nodes = await _load_nodes(conn, workspace_id)
    finally:
        await conn.close()
    return {"data": _workspace(row, nodes)}


@router.put("/v1/session-map/workspaces/{workspace_id}")
async def save_workspace(request: Request, workspace_id: str) -> dict[str, Any]:
    """整体保存（viewport + 全部节点）。地图的写热点是拖动，前端已做 800ms 防抖。"""
    try:
        body = await request.json()
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=400, detail="请求体不是合法 JSON") from exc
    payload = body or {}
    raw_nodes = payload.get("nodes") or []
    if not isinstance(raw_nodes, list):
        raise HTTPException(status_code=400, detail="nodes 必须是数组")
    if len(raw_nodes) > MAX_NODES:
        raise HTTPException(status_code=400, detail=f"nodes 超过上限 {MAX_NODES}")

    conn = await _connect(request)
    try:
        async with conn.transaction():
            row = await _require_workspace(conn, workspace_id)
            updated = await conn.fetchrow(
                f"UPDATE local_session_map_workspaces SET name = $2, viewport = $3::jsonb,"
                f" updated_at = now() WHERE id = $1 RETURNING {_WS_COLUMNS}",
                workspace_id,
                str(payload.get("name") or row["name"] or ""),
                json.dumps(_jsonable(payload.get("viewport")), ensure_ascii=False),
            )
            await conn.execute("DELETE FROM local_session_map_nodes WHERE workspace_id = $1", workspace_id)
            for index, raw in enumerate(raw_nodes):
                if not isinstance(raw, dict):
                    continue
                await conn.execute(
                    "INSERT INTO local_session_map_nodes"
                    " (id, workspace_id, session_id, parent_node_id, edge_kind, x, y, title,"
                    "  color, collapsed, hidden, pinned, branch_from_seq)"
                    " VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13)",
                    str(raw.get("id") or f"node_{workspace_id}_{index}"),
                    workspace_id,
                    str(raw.get("session_id") or "") or None,
                    str(raw.get("parent_node_id") or "") or None,
                    str(raw.get("edge_kind") or "manual"),
                    int(raw.get("x") or 0),
                    int(raw.get("y") or 0),
                    str(raw.get("title") or ""),
                    str(raw.get("color") or ""),
                    bool(raw.get("collapsed")),
                    bool(raw.get("hidden")),
                    bool(raw.get("pinned")),
                    raw.get("branch_from_seq") if isinstance(raw.get("branch_from_seq"), int) else None,
                )
            nodes = await _load_nodes(conn, workspace_id)
    finally:
        await conn.close()
    return {"data": _workspace(updated, nodes)}


@router.delete("/v1/session-map/workspaces/{workspace_id}")
async def delete_workspace(request: Request, workspace_id: str) -> dict[str, Any]:
    conn = await _connect(request)
    try:
        result = await conn.execute(
            "DELETE FROM local_session_map_workspaces WHERE id = $1 AND tenant_id = $2 AND user_id = $3",
            workspace_id,
            DEFAULT_TENANT_ID,
            DEFAULT_USER_ID,
        )
    finally:
        await conn.close()
    if result.endswith(" 0"):
        raise HTTPException(status_code=404, detail=f"画布不存在：{workspace_id}")
    return {"data": {"deleted": workspace_id}}


@router.post("/v1/session-map/workspaces/{workspace_id}/fork-node")
async def fork_node(request: Request, workspace_id: str) -> dict[str, Any]:
    """在画布上放一个指向新会话的节点。

    真正的"分叉出会话"由宿主（ChatView 的 RPC）完成，这里只负责落一个节点，
    因此不需要复制消息 —— 与云端 ``ForkNode`` 的职责边界一致。
    """
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001
        body = {}
    payload = body or {}
    node_id = f"smn_{uuid.uuid4().hex}"
    conn = await _connect(request)
    try:
        await _require_workspace(conn, workspace_id)
        await conn.execute(
            "INSERT INTO local_session_map_nodes"
            " (id, workspace_id, session_id, parent_node_id, edge_kind, x, y, title, branch_from_seq)"
            " VALUES ($1,$2,$3,$4,'branch',$5,$6,$7,$8)",
            node_id,
            workspace_id,
            str(payload.get("session_id") or "") or None,
            str(payload.get("parent_node_id") or "") or None,
            int(payload.get("x") or 0),
            int(payload.get("y") or 0),
            str(payload.get("title") or ""),
            payload.get("branch_from_seq") if isinstance(payload.get("branch_from_seq"), int) else None,
        )
        row = await conn.fetchrow(
            f"SELECT {_NODE_COLUMNS} FROM local_session_map_nodes WHERE id = $1", node_id
        )
    finally:
        await conn.close()
    return {"data": _node(row)}
