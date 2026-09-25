"""jobs — 后台任务（对应 deepseek-harness dsh-tool-jobs）

run_in_background 将命令作为 tool_job 投递到 engine:tasks 队列（跨实例消费组），
立即返回 job_id；任意引擎实例的 worker 执行（job_runner.execute_tool_job），
结果写 Redis，job_output 跨实例可查，job_kill 可靠终止子进程。

多实例语义：任务执行/结果/取消均不依赖发起实例（Redis 共享）；Redis 不可用时
降级为本进程 asyncio task（单实例语义，兼容旧行为）。
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
import uuid
from typing import Any

from app.redis_keys import rkey
from app.tools.context import get_session_id
from app.tools.registry import registry

logger = logging.getLogger(__name__)

TASK_STREAM = rkey("engine:tasks")
_JOB_TTL_HOURS = 24

# 本地降级路径（Redis 不可用）用
_jobs: dict[str, asyncio.Task[Any]] = {}
_job_created_at: dict[str, float] = {}
_cleanup_started = False


async def _get_redis() -> Any:
    from app.redis_client import get_redis

    return await get_redis()


# ── 远程（队列）路径 ─────────────────────────────────────────────

def _job_id() -> str:
    return f"job_{uuid.uuid4().hex[:10]}"


async def _enqueue_tool_job(job_id: str, command: str, shell_key: str) -> bool:
    """投递 tool_job 到 engine:tasks；Redis 不可用/失败返回 False（调用方降级本地执行）。"""
    try:
        redis = await _get_redis()
    except Exception as exc:  # noqa: BLE001
        logger.info("tool_job redis unavailable, local fallback: %s", exc)
        redis = None
    if redis is None:
        return False
    msg = {
        "task_id": job_id,
        "task_type": "tool_job",
        "tenant_id": "",
        "payload": json.dumps(
            {"job_id": job_id, "command": command, "shell_key": shell_key},
            ensure_ascii=False,
        ),
        "created_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "retry_count": "0",
        "trace_id": "",
        "priority": "0",
        # 幂等键 = job_id:命令执行成功后重投同一 job 不再重复执行(副作用幂等);
        # 失败仍可重试(claim 只拒绝 completed)。
        "idempotency_key": f"tool_job:{job_id}",
        "deadline": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() + 3600)),
    }
    try:
        await redis.xadd(TASK_STREAM, msg, maxlen=100000)
        # meta 置 running，供 job_output 区分 running / unknown
        from app.tools.job_runner import JOB_RESULT_TTL, meta_key

        await redis.hset(meta_key(job_id), mapping={"status": "running"})
        await redis.expire(meta_key(job_id), JOB_RESULT_TTL)
        return True
    except Exception as exc:  # noqa: BLE001
        logger.warning("tool_job enqueue failed, falling back to local: %s", exc)
        return False


async def _local_run(job_id: str, command: str) -> asyncio.Task[Any]:
    """Redis 不可用时的本地降级执行（单实例语义）。"""
    from app.tools.job_runner import execute_tool_job

    task = asyncio.create_task(execute_tool_job(None, job_id, command))
    _jobs[job_id] = task
    _job_created_at[job_id] = time.monotonic()
    _ensure_cleanup_started()
    return task


# ── 工具接口 ─────────────────────────────────────────────────────

async def run_in_background(command: str) -> dict[str, Any]:
    """Run *command* in the background; returns a job id."""
    if not command.strip():
        return {"error": "command is required"}
    job_id = _job_id()
    shell_key = get_session_id() or "default"

    ok = await _enqueue_tool_job(job_id, command, shell_key)
    if not ok:
        # 队列/Redis 不可达：本进程执行（与旧版一致；job_output/job_kill 走本地表）
        await _local_run(job_id, command)

    return {
        "job_id": job_id,
        "status": "started",
        "note": "check with job_output(job_id)",
    }


async def job_output(job_id: str) -> dict[str, Any]:
    """Return the background job's result if finished, or its status.

    优先读 Redis 结果/元数据（跨实例可见）；Redis 不可用回查本地降级任务。
    """
    # 1) 远程（Redis）结果
    try:
        redis = await _get_redis()
    except Exception:  # noqa: BLE001
        redis = None
    if redis is not None:
        try:
            from app.tools.job_runner import meta_key, result_key

            rv = await redis.get(result_key(job_id))
            if rv:
                try:
                    data = json.loads(rv)
                except ValueError:
                    data = {}
                return {"job_id": job_id, "status": data.get("status", "completed"),
                        "output": data.get("output", ""), "exit_code": data.get("exit_code", 0)}
            mv = await redis.hget(meta_key(job_id), "status")
            if mv in (b"running", "running"):
                return {"job_id": job_id, "status": "running"}
        except Exception as exc:  # noqa: BLE001
            logger.warning("job_output redis query failed: %s", exc)

    # 2) 本地降级任务
    task = _jobs.get(job_id)
    if task is None:
        return {"error": f"unknown job: {job_id}"}
    if task.done():
        _jobs.pop(job_id, None)
        _job_created_at.pop(job_id, None)
        try:
            result = task.result()
        except asyncio.CancelledError:
            return {"job_id": job_id, "status": "cancelled"}
        except Exception as e:  # noqa: BLE001
            return {"job_id": job_id, "status": "failed", "error": str(e)}
        status = result.get("status", "completed")
        if status in ("completed", "failed", "cancelled", "blocked"):
            return {"job_id": job_id, "status": status,
                    "output": result.get("output", ""), "exit_code": result.get("exit_code", 0)}
        return {"job_id": job_id, "status": status, **result}
    return {"job_id": job_id, "status": "running"}


async def job_kill(job_id: str) -> dict[str, Any]:
    """Cancel a running background job（可靠终止其子进程）。"""
    # 1) 本地降级任务：直接 cancel（execute_tool_job 捕获 CancelledError 会 kill 子进程）
    task = _jobs.get(job_id)
    if task is not None:
        task.cancel()
        _jobs.pop(job_id, None)
        _job_created_at.pop(job_id, None)
        return {"job_id": job_id, "status": "cancelled"}

    # 2) 远程任务：置 Redis kill 标志，worker watcher 轮询后 kill 子进程
    try:
        redis = await _get_redis()
    except Exception:  # noqa: BLE001
        redis = None
    if redis is not None:
        from app.tools.job_runner import JOB_KILL_TTL, kill_key, meta_key

        # 先确认这个 job 真的存在。少了这一步，任意 job_id 都会走到下面、写一个 kill 标志
        # 然后返回 "cancelled" —— 调用方（和模型）会以为取消成功了，实际只是往一个没人读的
        # key 写了 1。job 是否存在以 job:meta:{id} 为准（worker 起任务时写入，
        # 见 job_runner._write_meta）。
        try:
            if not await redis.exists(meta_key(job_id)):
                return {"error": f"unknown job: {job_id}"}
        except Exception as exc:  # noqa: BLE001 - 查不通存在性时不敢假装成功
            logger.warning("job_kill existence check failed: %s", exc)
            return {"error": f"kill failed: {exc}"}

        try:
            await redis.set(kill_key(job_id), "1", ex=JOB_KILL_TTL)
            return {"job_id": job_id, "status": "cancelled"}
        except Exception as exc:  # noqa: BLE001
            logger.warning("job_kill flag write failed: %s", exc)
            return {"error": f"kill failed: {exc}"}

    return {"error": f"unknown job: {job_id}"}


# ── 本地降级任务的清理循环（仅本进程 _jobs；远程任务由 Redis TTL 回收）──

async def _start_cleanup_loop() -> None:
    """Periodically remove stale completed local tasks to prevent memory leaks."""
    global _cleanup_started
    if _cleanup_started:
        return
    _cleanup_started = True
    while True:
        await asyncio.sleep(3600)  # every hour
        now = time.monotonic()
        stale_ids = []
        for job_id, task in list(_jobs.items()):
            created = _job_created_at.get(job_id, now)
            age_hours = (now - created) / 3600
            if task.done() and age_hours > _JOB_TTL_HOURS:
                try:
                    task.result()
                except Exception:  # noqa: BLE001
                    pass
                stale_ids.append(job_id)
            elif not task.done() and age_hours > 48:
                task.cancel()
                stale_ids.append(job_id)
        for job_id in stale_ids:
            _jobs.pop(job_id, None)
            _job_created_at.pop(job_id, None)
        if stale_ids:
            logger.info("cleaned up %d stale local background jobs", len(stale_ids))


def _ensure_cleanup_started() -> None:
    asyncio.create_task(_start_cleanup_loop())


registry.register(
    name="run_in_background",
    description="Run a command in the background and get a job id immediately. Poll with job_output, stop with job_kill.",
    parameters={
        "type": "object",
        "properties": {"command": {"type": "string"}},
        "required": ["command"],
    },
    handler=run_in_background,
)

registry.register(
    name="job_output",
    description="Fetch a background job's result (completed) or its status (running).",
    parameters={
        "type": "object",
        "properties": {"job_id": {"type": "string"}},
        "required": ["job_id"],
    },
    handler=job_output,
)

registry.register(
    name="job_kill",
    description="Cancel a running background job.",
    parameters={
        "type": "object",
        "properties": {"job_id": {"type": "string"}},
        "required": ["job_id"],
    },
    handler=job_kill,
)
