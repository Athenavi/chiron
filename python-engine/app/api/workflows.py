"""Graph/Workflow API endpoints — CRUD + execution with PostgreSQL persistence."""

from __future__ import annotations

import asyncio
import datetime
import json
import logging
import uuid
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel

from app.config import settings
from app.db import get_pool
from app.main import get_gateway
from app.redis_keys import rkey
from app.workflow.engine import get_instance
from app.workflow.executor import execute_with_checkpoint

logger = logging.getLogger(__name__)

router = APIRouter(tags=["graphs"])

# 保存后台任务引用，防止被 GC 回收导致工作流静默丢失（asyncio.create_task 必须持有引用）
_background_tasks: set[asyncio.Task[Any]] = set()


class GraphCreateRequest(BaseModel):
    id: str | None = None
    name: str
    graph_json: Any = {}
    user_id: str | None = None


class GraphExecuteRequest(BaseModel):
    name: str = "workflow"
    nodes: list[dict[str, Any]] = []
    edges: list[dict[str, Any]] = []
    initial_state: dict[str, Any] = {}


async def _enqueue_with_retry(
    instance_id: str, user_id: str, graph_json: dict[str, Any], initial_state: dict[str, Any], *, attempts: int = 3
) -> bool:
    """投递到 engine:tasks，失败重试（指数退避，总等待 <3s）。

    重试值得做：入队失败的主因是 Redis/队列**瞬时**不可达，一次失败不代表这个任务注定跑不了。
    """
    for attempt in range(1, attempts + 1):
        if await _enqueue_workflow_run(instance_id, user_id, graph_json, initial_state):
            if attempt > 1:
                logger.info("workflow enqueue succeeded on attempt %d: %s", attempt, instance_id)
            return True
        if attempt < attempts:
            await asyncio.sleep(min(0.2 * (2 ** (attempt - 1)), 1.0))
    return False


async def _mark_enqueue_pending(instance_id: str, graph_json: dict[str, Any]) -> None:
    """标记为待执行，并把重建入队所需的 graph 一并落库。

    **存 graph 不是可选项**：``graph_json`` 只存在于这次请求里；不存的话 requeue 时无法重建
    入队参数，那"标记待执行"就只是把"任务丢了"从不可见变成可见而已。
    """
    try:
        pool = get_pool()
        await pool.execute(
            """UPDATE workflow_instances
                  SET status = 'queued_pending', results = $2::json, error = $3, updated_at = NOW()
                WHERE id = $1""",
            instance_id,
            json.dumps({"_pending_graph": graph_json}),
            "enqueue failed after retries (Redis/queue unavailable); awaiting requeue",
        )
    except Exception as e:  # noqa: BLE001 - 标记失败只记日志；实例仍是 running，可人工介入
        logger.error("mark workflow queued_pending failed (%s): %s", instance_id, e)


async def requeue_pending_workflows(limit: int = 20) -> int:
    """把 ``queued_pending`` 的实例重新入队；返回成功条数。

    调用时机：**服务启动时一次**。入队失败的主因是 Redis/队列暂不可达，进程重启是最自然的
    重试点。真正的周期 reconciler 属队列管理面（文档 X6）；这里先补最小闭环 —— 否则
    "标记待执行"等于这个任务永远不跑。
    """
    try:
        pool = get_pool()
        rows = await pool.fetch(
            """SELECT id, COALESCE(user_id, '') AS user_id, results
                 FROM workflow_instances
                WHERE status = 'queued_pending'
                ORDER BY created_at
                LIMIT $1""",
            limit,
        )
    except Exception as e:  # noqa: BLE001 - 扫描失败不影响启动
        logger.warning("workflow requeue scan failed: %s", e)
        return 0

    requeued = 0
    for row in rows:
        raw = row["results"]
        if isinstance(raw, str):
            try:
                raw = json.loads(raw)
            except Exception:  # noqa: BLE001
                raw = {}
        graph = raw.get("_pending_graph") if isinstance(raw, dict) else None
        if not isinstance(graph, dict) or not graph:
            continue  # 无法重建入队（老数据）：保持 pending，继续可见
        if not await _enqueue_with_retry(row["id"], row["user_id"] or "", graph, {}):
            continue
        try:
            await pool.execute(
                """UPDATE workflow_instances
                      SET status = 'running', error = NULL, updated_at = NOW()
                    WHERE id = $1 AND status = 'queued_pending'""",
                row["id"],
            )
            requeued += 1
        except Exception as e:  # noqa: BLE001
            logger.warning("workflow requeue status update failed (%s): %s", row["id"], e)
    if requeued:
        logger.info("requeued %d pending workflow instance(s)", requeued)
    return requeued


async def _enqueue_workflow_run(
    instance_id: str, user_id: str, graph_json: dict[str, Any], initial_state: dict[str, Any]
) -> bool:
    """投递 workflow_run 到 engine:tasks（跨实例消费组，断点续跑执行）。失败返回 False。"""
    from app.redis_client import get_redis

    try:
        redis = await get_redis()
    except Exception:  # noqa: BLE001
        redis = None
    if redis is None:
        return False
    msg: dict[Any, Any] = {
        "task_id": instance_id,
        "task_type": "workflow_run",
        "tenant_id": "",
        "payload": json.dumps(
            {
                "instance_id": instance_id,
                "user_id": user_id,
                "graph_json": graph_json,
                "initial_state": initial_state,
            },
            ensure_ascii=False,
        ),
        "created_at": datetime.datetime.now(datetime.UTC).strftime(
            "%Y-%m-%dT%H:%M:%SZ"
        ),
        "retry_count": "0",
        "trace_id": "",
        "priority": "0",
        # 幂等键 = instance_id:workflow 断点续跑幂等(executor 依 checkpoint);
        # 已完成实例重投不再执行。
        "idempotency_key": f"workflow_run:{instance_id}",
        "deadline": (
            datetime.datetime.now(datetime.UTC) + datetime.timedelta(hours=2)
        ).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }
    try:
        await redis.xadd(rkey("engine:tasks"), msg, maxlen=100000)
        return True
    except Exception as exc:  # noqa: BLE001
        logger.warning("workflow_run enqueue failed: %s", exc)
        return False


@router.get("/v1/graphs")
async def list_graphs(user_id: str | None = Query(None, alias="user_id")) -> dict[str, Any]:
    """List all graphs, optionally filtered by user_id."""
    try:
        pool = get_pool()
        if user_id:
            rows = await pool.fetch(
                """SELECT id, name, COALESCE(user_id::VARCHAR,'') as user_id, graph_json, created_at, updated_at
                   FROM workflow_graphs WHERE user_id::VARCHAR = $1
                   ORDER BY updated_at DESC NULLS LAST, created_at DESC LIMIT 100""",
                str(user_id),
            )
        else:
            rows = await pool.fetch(
                """SELECT id, name, COALESCE(user_id::VARCHAR,'') as user_id, graph_json, created_at, updated_at
                   FROM workflow_graphs
                   ORDER BY updated_at DESC NULLS LAST, created_at DESC LIMIT 100"""
            )
        results = [dict(r) for r in rows]
        for r in results:
            if isinstance(r.get("graph_json"), str):
                r["graph_json"] = json.loads(r["graph_json"])
            r["created_at"] = r["created_at"].isoformat() if r.get("created_at") else ""
            r["updated_at"] = r["updated_at"].isoformat() if r.get("updated_at") else ""
        return {"success": True, "data": results}
    except Exception as e:
        logger.warning("graph list failed: %s", e)
        return {"success": False, "error": str(e), "data": []}


@router.post("/v1/graphs")
async def create_graph(
    body: GraphCreateRequest, user_id: str = Query("", alias="user_id")
) -> dict[str, Any]:
    """Create or update a graph definition."""
    import uuid as _uuid

    # 优先使用请求体中的 user_id，再回退到查询参数
    effective_user_id = body.user_id or user_id
    if not effective_user_id:
        raise HTTPException(status_code=400, detail="user_id is required")

    graph_id = body.id or f"g-{_uuid.uuid4().hex[:10]}"
    now = datetime.datetime.now(datetime.UTC)

    graph_json = body.graph_json
    if isinstance(graph_json, str):
        try:
            graph_json = json.loads(graph_json)
        except json.JSONDecodeError:
            pass

    record = {
        "id": graph_id,
        "name": body.name,
        "user_id": effective_user_id,
        "graph_json": graph_json,
        "created_at": now.isoformat(),
        "updated_at": now.isoformat(),
    }

    try:
        pool = get_pool()
        await pool.execute(
            """INSERT INTO workflow_graphs (id, name, user_id, graph_json, created_at, updated_at)
               VALUES ($1, $2, $3, $4, $5, $6)
               ON CONFLICT (id) DO UPDATE SET name = EXCLUDED.name, graph_json = EXCLUDED.graph_json, updated_at = EXCLUDED.updated_at""",
            graph_id,
            body.name,
            effective_user_id or None,
            json.dumps(graph_json),
            now,
            now,
        )
    except Exception as e:
        logger.error("graph insert failed: %s", e)
        raise HTTPException(status_code=500, detail="failed to save graph") from e

    return {"success": True, "data": record}


@router.get("/v1/graphs/{graph_id}")
async def get_graph(graph_id: str) -> dict[str, Any]:
    """Get a graph by ID."""
    try:
        pool = get_pool()
        row = await pool.fetchrow(
            """SELECT id, name, COALESCE(user_id,'') as user_id, graph_json, created_at, updated_at
               FROM workflow_graphs WHERE id = $1""",
            graph_id,
        )
        if row:
            record = dict(row)
            if isinstance(record.get("graph_json"), str):
                record["graph_json"] = json.loads(record["graph_json"])
            record["created_at"] = (
                record["created_at"].isoformat() if record.get("created_at") else ""
            )
            record["updated_at"] = (
                record["updated_at"].isoformat() if record.get("updated_at") else ""
            )
            return {"success": True, "data": record}
    except Exception as e:
        logger.warning("graph get failed: %s", e)

    raise HTTPException(status_code=404, detail="graph not found")


@router.delete("/v1/graphs/{graph_id}")
async def delete_graph(graph_id: str) -> dict[str, Any]:
    """Delete a graph by ID."""
    if not graph_id or graph_id.strip() == "":
        raise HTTPException(status_code=400, detail="graph_id is required")
    try:
        pool = get_pool()
        result = await pool.execute(
            "DELETE FROM workflow_graphs WHERE id = $1", graph_id
        )
        if result == "DELETE 0":
            raise HTTPException(status_code=404, detail="graph not found")
    except HTTPException:
        raise
    except Exception as e:
        logger.error("graph delete failed: %s", e)
        raise HTTPException(status_code=500, detail="failed to delete graph") from e

    return {"success": True, "message": f"Graph {graph_id} deleted"}


@router.post("/v1/graphs/{graph_id}/execute")
async def execute_graph(
    graph_id: str,
    body: GraphExecuteRequest,
    gateway: Any = Depends(get_gateway),
    user_id: str = Query("", alias="user_id"),
) -> dict[str, Any]:
    """提交图执行：落库 pending 后后台任务执行，立即返回 instance_id（前端轮询状态）。"""
    graph_json = None

    # 尝试从 PostgreSQL 加载
    try:
        pool = get_pool()
        row = await pool.fetchrow(
            "SELECT graph_json FROM workflow_graphs WHERE id = $1",
            graph_id,
        )
        if row:
            graph_json = row["graph_json"]
            if isinstance(graph_json, str):
                graph_json = json.loads(graph_json)
    except Exception as e:
        logger.warning("graph load for execute failed: %s", e)

    # Fallback to request body
    if not graph_json:
        graph_json = {
            "name": body.name or graph_id,
            "nodes": body.nodes,
            "edges": body.edges,
        }

    graph_name = graph_json.get("name") or graph_id
    instance_id = f"wf_{uuid.uuid4().hex[:10]}"
    now = datetime.datetime.now(datetime.UTC)

    # 落库 running（后台任务完成后更新）
    try:
        pool = get_pool()
        await pool.execute(
            """INSERT INTO workflow_instances (id, user_id, workflow_id, workflow_name, status, results, created_at, updated_at)
               VALUES ($1, $2, $3, $4, 'running', '{}', $5, $5)""",
            instance_id,
            user_id,
            graph_id,
            graph_name,
            now,
        )
    except Exception as e:
        logger.warning("workflow instance insert failed: %s", e)

    # 断点续跑：投递 engine:tasks 由队列 worker 消费执行（executor 幂等 + checkpoint 续跑）。
    #
    # 入队失败**不能静默降级为本地执行**：本地跑不持久化，副本重启即丢，而接口已经返回
    # "running" —— 用户以为任务在跑，其实它随进程一起消失了（"看起来成功、实际丢失"）。
    # 改为：重试入队；仍失败则把实例标记为**待执行**（连同重建入队所需的 graph 一起落库），
    # 由 requeue 机制拉起，并打点告警 —— 降级必须可见。
    if not await _enqueue_with_retry(instance_id, user_id, graph_json, body.initial_state):
        await _mark_enqueue_pending(instance_id, graph_json)
        try:
            from app.observability.metrics import WORKFLOW_ENQUEUE_PENDING

            WORKFLOW_ENQUEUE_PENDING.inc()
        except Exception:  # noqa: BLE001 - 指标不可用不该影响主流程
            pass
        logger.error(
            "workflow enqueue failed after retries; marked queued_pending: %s", instance_id
        )
        # 兜底开关（默认关）：确实需要时退回本进程执行，但**明确告警它不是持久化的**。
        if settings.workflow_local_fallback:
            logger.warning(
                "workflow_local_fallback enabled — running in-process (NOT persisted): %s",
                instance_id,
            )
            task = asyncio.create_task(
                execute_with_checkpoint(
                    instance_id, graph_json, body.initial_state, user_id, gateway
                )
            )
            _background_tasks.add(task)
            task.add_done_callback(_background_tasks.discard)

    return {"instance_id": instance_id, "status": "running", "workflow": graph_name}


@router.get("/v1/workflows/instances")
async def list_instances(user_id: str = Query("", alias="user_id")) -> dict[str, Any]:
    """持久化的工作流执行历史（按用户）。"""
    try:
        pool = get_pool()
        if user_id:
            rows = await pool.fetch(
                """SELECT id, workflow_id, workflow_name, status, results, COALESCE(error,'') as error, created_at, updated_at
                   FROM workflow_instances WHERE user_id = $1
                   ORDER BY created_at DESC LIMIT 100""",
                user_id,
            )
        else:
            rows = await pool.fetch(
                """SELECT id, workflow_id, workflow_name, status, results, COALESCE(error,'') as error, created_at, updated_at
                   FROM workflow_instances ORDER BY created_at DESC LIMIT 100"""
            )
        out = []
        for r in rows:
            d = dict(r)
            if isinstance(d.get("results"), str):
                try:
                    d["results"] = json.loads(d["results"])
                except json.JSONDecodeError:
                    d["results"] = {}
            d["created_at"] = d["created_at"].isoformat() if d.get("created_at") else ""
            d["updated_at"] = d["updated_at"].isoformat() if d.get("updated_at") else ""
            out.append(d)
        return {"success": True, "data": out}
    except Exception as e:
        logger.warning("workflow instances list failed: %s", e)
        return {"success": False, "error": str(e), "data": []}


@router.get("/v1/workflows/{instance_id}/status")
async def workflow_status(instance_id: str) -> dict[str, Any]:
    """获取工作流执行状态（内存实例优先，落库记录回退）。"""
    inst = get_instance(instance_id)
    if inst:
        return {
            "instance_id": inst.instance_id,
            "workflow": inst.graph_name,
            "status": inst.status,
            "results": {
                nid: {"status": nr.status, "output": nr.output}
                for nid, nr in inst.results.items()
            },
        }
    try:
        pool = get_pool()
        row = await pool.fetchrow(
            """SELECT id, workflow_name, status, results, COALESCE(error,'') as error
               FROM workflow_instances WHERE id = $1""",
            instance_id,
        )
        if row:
            results = row["results"]
            if isinstance(results, str):
                try:
                    results = json.loads(results)
                except json.JSONDecodeError:
                    results = {}
            return {
                "instance_id": row["id"],
                "workflow": row["workflow_name"],
                "status": row["status"],
                "results": results,
                "error": row["error"] or "",
            }
    except Exception as e:
        logger.warning("workflow status db fallback failed: %s", e)
    raise HTTPException(status_code=404, detail="workflow instance not found")
