"""tool_job 执行器 —— 以独立一次性子进程运行后台命令（与 PersistentTerminal 解耦）。

设计（多引擎实例无缝扩展）：
- run_in_background 的任务经 engine:tasks 队列分发（消费组 + 全局门控），任意 worker 实例可执行；
- 结果写 Redis `job:result:{job_id}`（TTL 24h），任意实例的 job_output 都可读取（跨实例可见）；
- job_kill 置 Redis `job:kill:{job_id}` 标志，worker 的 watcher 发现后 kill 子进程 —— 可靠终止；
- 后台命令运行在独立子进程（bash -c / cmd /c），不复用同 session 的前台持久 shell，
  因此 kill 后台任务不会干扰/重置前台终端的 shell 状态；
- Redis 不可用时由 tools/jobs.py 本地降级调用 execute_tool_job(redis=None, ...)（单实例语义）。
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import sys
from typing import Any

from app.redis_keys import rkey

logger = logging.getLogger(__name__)

JOB_RESULT_TTL = 24 * 3600  # 结果保留时长（秒）
JOB_KILL_TTL = 120  # 取消标志有效期（秒）；进程被 kill 后残留标志由完成路径清理
KILL_POLL_INTERVAL = 0.5  # watcher 轮询取消标志间隔（秒）
JOB_TIMEOUT = 3600  # 单条后台命令上限（与原 run_in_background timeout 一致）


def meta_key(job_id: str) -> str:
    return rkey(f"job:meta:{job_id}")


def result_key(job_id: str) -> str:
    return rkey(f"job:result:{job_id}")


def kill_key(job_id: str) -> str:
    return rkey(f"job:kill:{job_id}")


def _shell_cmd() -> list[str]:
    if sys.platform == "win32":
        return [os.environ.get("COMSPEC", "cmd.exe"), "/c"]
    return [os.environ.get("SHELL", "/bin/bash"), "-c"]


async def _kill_proc(proc: asyncio.subprocess.Process) -> None:
    if proc.returncode is None:
        try:
            proc.kill()
            await asyncio.wait_for(proc.wait(), timeout=5)
        except Exception:  # noqa: BLE001
            pass


async def _kill_watcher(redis: Any, job_id: str, proc: asyncio.subprocess.Process) -> None:
    """轮询取消标志；发现后 kill 子进程（Redis 故障则静默退出，命令继续自然执行）。"""
    while True:
        await asyncio.sleep(KILL_POLL_INTERVAL)
        try:
            v = await redis.get(kill_key(job_id))
        except Exception:  # noqa: BLE001
            return
        if v in (b"1", "1", 1):
            logger.info("tool_job kill flag observed, killing process", extra={"job_id": job_id})
            await _kill_proc(proc)
            return


async def _write_meta(redis: Any, job_id: str, status: str) -> None:
    if redis is None:
        return
    try:
        await redis.hset(meta_key(job_id), mapping={"status": status})
        await redis.expire(meta_key(job_id), JOB_RESULT_TTL)
    except Exception as exc:  # noqa: BLE001
        logger.warning("tool_job meta write failed: %s", exc)


async def _write_result(redis: Any, job_id: str, status: str, output: str = "", exit_code: int = 0) -> None:
    if redis is None:
        return
    try:
        await redis.set(
            result_key(job_id),
            json.dumps({"job_id": job_id, "status": status, "output": output, "exit_code": exit_code}, ensure_ascii=False),
            ex=JOB_RESULT_TTL,
        )
        await redis.delete(kill_key(job_id))
    except Exception as exc:  # noqa: BLE001
        logger.warning("tool_job result write failed: %s", exc)


async def execute_tool_job(
    redis: Any,
    job_id: str,
    command: str,
    timeout: float = JOB_TIMEOUT,
) -> dict[str, Any]:
    """执行一条后台命令并写终态，返回 {job_id, status, output?, exit_code?}。

    status: completed / failed / cancelled / blocked。
    - 取消（Redis kill 标志 或 调用方 task.cancel）都会终止子进程；
    - 命令被显式 kill（exit_code<0 且 kill 标志存在）记为 cancelled。
    """
    from app.tools.sandbox import _has_escape, sandboxed_env, workspace_dir

    # 逃逸拦截：与 terminal/shell_exec 同一套规则（沙箱安全）
    esc = _has_escape(command)
    if esc:
        msg = f"[blocked: {esc}] command not allowed in sandbox"
        await _write_meta(redis, job_id, "completed")
        await _write_result(redis, job_id, "blocked", msg, -1)
        return {"job_id": job_id, "status": "blocked", "output": msg, "exit_code": -1}

    await _write_meta(redis, job_id, "running")
    proc = await asyncio.create_subprocess_exec(
        *_shell_cmd(),
        command,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
        cwd=str(workspace_dir()),
        env=sandboxed_env(),
    )
    watcher: asyncio.Task[Any] | None = None
    if redis is not None:
        watcher = asyncio.create_task(_kill_watcher(redis, job_id, proc))

    status = "completed"
    exit_code = 0
    output = ""
    try:
        out_b, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        output = out_b.decode("utf-8", errors="replace")
        exit_code = proc.returncode or 0
        # 被 kill 判定：退出码 <0 且存在取消标志（Redis 可用时）
        killed = False
        if exit_code < 0 and redis is not None:
            try:
                killed = await redis.get(kill_key(job_id)) in (b"1", "1", 1)
            except Exception:  # noqa: BLE001
                killed = False
        if killed:
            status = "cancelled"
        elif exit_code != 0:
            status = "failed"
    except TimeoutError:
        status = "failed"
        await _kill_proc(proc)
        output = f"[timed out after {timeout}s]"
        exit_code = -1
    except asyncio.CancelledError:
        status = "cancelled"
        await _kill_proc(proc)
        raise
    finally:
        if watcher is not None:
            watcher.cancel()

    if status == "cancelled":
        await _write_meta(redis, job_id, "cancelled")
        await _write_result(redis, job_id, "cancelled", output or "", exit_code)
    else:
        await _write_meta(redis, job_id, status)
        await _write_result(redis, job_id, status, output, exit_code)
    return {"job_id": job_id, "status": status, "output": output, "exit_code": exit_code}
