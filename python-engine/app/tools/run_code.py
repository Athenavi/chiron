"""run_code 工具 — PTC 模式核心（对应 deepseek-harness Code Mode SDK）

模型编写一段 Python 异步程序，通过注入的 ``tools`` 命名空间组合调用多个
工具（``await tools.read_file(...)``、``await tools.shell_exec(...)`` 等），
一次执行多步操作（deepseek: "five round trips become one"）。

- 程序体为 ``async def _main():`` 的函数体
- ``tools`` 是注入的命名空间：每个已注册工具的异步方法，调用
  ``registry.execute(name, args)``，返回规范 JSON；工具失败抛
  ``ToolCallError(name, message)``
- stdout 捕获 → logs；``return`` 值 → result
- 超时/异常 → 结构化 ``{isError, message, logs}`` 返回模型自纠

S5 沙箱隔离（subprocess + IPC 代理）：
- 模型代码在独立子进程内执行，通过 stdin/stdout JSON-line 协议
  代理 tools 调用回主进程；子进程无直接访问宿主文件/socket/DB 的能力
- 子进程应用 RLIMIT_AS/CPU/FSIZE（POSIX）+ 主进程 wall-clock SIGKILL
- 静态守卫（code_guard.check_static）在子进程内再次执行（纵深防御） +
  封禁 __class__/__subclasses__ 等对象内省逃逸链
- S 修复：子进程不可用时 fail-closed 报错，绝不在宿主进程直接 exec 用户代码
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import json
import logging
import subprocess
import sys
import threading
import time
import types
from typing import Any, cast

from app.tools.code_guard import (
    check_static as _check_static,
)
from app.tools.code_guard import (
    safe_builtins as _safe_builtins,
)
from app.tools.registry import registry

# 这两个符号以 ``_`` 前缀从 code_guard 转出，供 app/skill/manager.py 与本模块共用
# （见 manager.py 的 `from app.tools.run_code import _check_static, _safe_builtins`）。
# 显式登记一次，避免 Ruff F401 把「转给他人用」误判成「未使用」而删除。
__all__ = [
    "run_code",
    "sdk_usage_text",
    "_check_static",
    "_render_result",
    "_safe_builtins",
]

logger = logging.getLogger(__name__)

DEFAULT_TIMEOUT_SECONDS = 60
MAX_LOG_CHARS = 20_000


class ToolCallError(Exception):
    """模型程序内调用工具失败时抛出的错误（仅携带 toolName + message）。"""

    def __init__(self, tool_name: str, message: str):
        self.tool_name = tool_name
        self.message = message
        super().__init__(f"ToolCallError({tool_name}): {message}")


async def _dispatch(name: str, args: dict[str, Any]) -> Any:
    """调用一个已注册工具，失败抛 ToolCallError。"""
    result = await registry.execute(name, args)
    if isinstance(result, dict) and result.get("error"):
        raise ToolCallError(name, str(result["error"]))
    return result


def _build_sdk() -> types.SimpleNamespace:
    """构造 tools 命名空间：每个注册工具一个 async 方法（同进程降级模式用）。"""
    ns = types.SimpleNamespace()

    async def _make_tool(name: str, *args: Any, **kwargs: Any) -> Any:
        # 兼容 tools.name(args_dict) 与 tools.name(k=v) 两种调用
        if len(args) == 1 and isinstance(args[0], dict) and not kwargs:
            params: dict[str, Any] = args[0]
        else:
            params = kwargs
        return await _dispatch(name, params)

    for tool_name in registry.list_names():
        setattr(ns, tool_name, _make_tool.__get__(tool_name, None))
    return ns


def sdk_usage_text() -> str:
    """生成注入 prompt 的 SDK 用法说明（确定性、按当前注册表）。"""
    names = ", ".join(sorted(registry.list_names()))
    return (
        "In the run_code program, a `tools` namespace is available: "
        "each registered tool is an async callable, e.g. "
        "`result = await tools.read_file(path='src/main.py')` or "
        "`result = await tools.grep_files(pattern='import', path='src')`. "
        "Tool failures raise ToolCallError(name, message). "
        "Return a JSON-serializable value from the program; captured stdout "
        "appears in logs. Available tools: " + names
    )


def _render_result(value: Any) -> Any:
    """规范化返回值（json-serializable），限制递归深度防栈溢出。"""
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    try:
        return json.loads(
            json.dumps(
                value, ensure_ascii=False, default=str, check_circular=True, indent=None
            )
        )
    except (ValueError, RecursionError, OverflowError):
        return str(value)


# ── subprocess 隔离模式 ──────────────────────────────────────────────


def _resolve_worker_cmd() -> list[str] | None:
    """构造启动 _sandbox_worker 子进程的命令。失败时返回 None（触发降级）。"""
    # 优先以模块方式启动：python -m app.tools._sandbox_worker
    # 要求主进程在 python-engine/ 目录下启动（与现有部署一致）
    return [sys.executable, "-m", "app.tools._sandbox_worker"]


def _run_subprocess_sync(
    code: str,
    tool_names: list[str],
    timeout: int,
    loop: asyncio.AbstractEventLoop,
    context_snapshot: dict[str, Any] | None,
) -> dict[str, Any] | None:
    """同步子进程执行器（在 executor 线程中运行）。

    用 subprocess.Popen + 阻塞 readline 主循环；工具调用通过
    ``asyncio.run_coroutine_threadsafe`` 调度回主 asyncio loop 执行
    registry.execute，避免 Windows ProactorEventLoop 下 asyncio.subprocess
    的 buffering 问题（同步 subprocess 的 stdout 行缓冲更可靠）。
    """
    cmd = _resolve_worker_cmd()
    if cmd is None:
        return None

    try:
        from app.tools.sandbox import sandboxed_env

        # S 安全修复:子进程清理宿主 env(API key/JWT_SECRET 等),仅保留基础变量,
        # 防止沙箱内模型代码 `os.environ` 外带密钥。
        proc = subprocess.Popen(
            cmd,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,  # 行缓冲
            env=sandboxed_env(),
        )
        # stdin/stdout 都显式用了 PIPE，这里只是把类型收窄（Popen 的返回类型是
        # IO[str] | None）。真为 None 时下面的写入本来就会 AttributeError，
        # 所以断言不改变任何可达路径的行为。
        assert proc.stdin is not None and proc.stdout is not None
    except (OSError, FileNotFoundError) as e:
        logger.warning("subprocess start failed, will fallback to in-process: %s", e)
        return None
    except Exception as e:  # noqa: BLE001 — 任何启动异常都降级
        logger.warning("subprocess start unexpected error, will fallback: %s", e)
        return None

    deadline = time.time() + timeout

    # P0 安全修复：启动后台 stderr 消费线程
    # 如果 stderr 管道被填满，子进程会永久阻塞。后台线程持续消费 stderr 防止此问题。
    stderr_buffer: list[str] = []
    stderr_lock = threading.Lock()

    def _drain_stderr() -> None:
        try:
            if proc.stderr:
                for line in proc.stderr:
                    with stderr_lock:
                        stderr_buffer.append(line)
        except Exception:
            pass

    stderr_thread = threading.Thread(target=_drain_stderr, daemon=True, name="stderr_drain")
    stderr_thread.start()

    try:
        # 1) 写 init 消息
        init_msg = (
            json.dumps(
                {
                    "type": "init",
                    "code": code,
                    "tools": tool_names,
                },
                ensure_ascii=False,
            )
            + "\n"
        )
        try:
            proc.stdin.write(init_msg)
            proc.stdin.flush()
        except (BrokenPipeError, OSError) as e:
            _kill_proc_sync(proc)
            stderr_thread.join(timeout=1.0)
            with stderr_lock:
                stderr = "".join(stderr_buffer)[:500]
            return {
                "isError": True,
                "message": f"subprocess stdin write failed: {e}; stderr: {stderr}",
                "logs": "",
            }

        # 2) 主循环：阻塞读 stdout，处理 tool_call，写 tool_result
        while True:
            line = _readline_with_deadline(proc, deadline)
            if line is None:
                # 超时
                _kill_proc_sync(proc)
                stderr_thread.join(timeout=1.0)
                return {
                    "isError": True,
                    "message": f"subprocess timed out after {timeout}s",
                    "logs": "",
                }
            if line == "":
                # EOF：子进程退出但无 done/error
                stderr_thread.join(timeout=1.0)
                with stderr_lock:
                    stderr = "".join(stderr_buffer)[:500]
                return {
                    "isError": True,
                    "message": f"subprocess exited unexpectedly: {stderr}",
                    "logs": "",
                }

            try:
                msg = json.loads(line)
            except json.JSONDecodeError as e:
                logger.warning(
                    "invalid IPC line from subprocess: %s (line=%r)", e, line[:200]
                )
                continue

            mtype = msg.get("type")
            if mtype == "tool_call":
                call_id = msg.get("call_id")
                name = msg.get("name", "")
                args = msg.get("args", {}) or {}

                # 调度 async registry.execute 到主 asyncio loop（带 contextvar 快照）
                future = asyncio.run_coroutine_threadsafe(
                    _safe_tool_call(name, args, context_snapshot),
                    loop,
                )
                try:
                    result_obj = future.result(timeout=max(1, deadline - time.time()))
                except Exception as e:  # noqa: BLE001 — 工具调用失败
                    if time.time() >= deadline:
                        _kill_proc_sync(proc)
                        return {
                            "isError": True,
                            "message": f"tool call timed out (exceeded overall {timeout}s deadline)",
                            "logs": "",
                        }
                    resp: dict[str, Any] = {
                        "type": "tool_error",
                        "call_id": call_id,
                        "error": f"{type(e).__name__}: {e}",
                    }
                else:
                    if isinstance(result_obj, dict) and result_obj.get("error"):
                        resp = {
                            "type": "tool_error",
                            "call_id": call_id,
                            "error": str(result_obj["error"]),
                        }
                    else:
                        resp = {
                            "type": "tool_result",
                            "call_id": call_id,
                            "result": _render_result(result_obj),
                        }

                try:
                    proc.stdin.write(
                        json.dumps(resp, ensure_ascii=False, default=str) + "\n"
                    )
                    proc.stdin.flush()
                except (BrokenPipeError, OSError):
                    return {
                        "isError": True,
                        "message": "subprocess died during tool call",
                        "logs": "",
                    }
            elif mtype == "done":
                _wait_proc_sync(proc)
                return {
                    "logs": str(msg.get("logs", ""))[-MAX_LOG_CHARS:],
                    "result": _render_result(msg.get("result")),
                }
            elif mtype == "error":
                _wait_proc_sync(proc)
                return {
                    "isError": True,
                    "message": str(msg.get("message", "unknown subprocess error")),
                    "logs": str(msg.get("logs", ""))[-MAX_LOG_CHARS:],
                }
            else:
                logger.warning("unknown IPC message type: %s", mtype)
    except Exception as e:  # noqa: BLE001 — 主循环意外异常
        _kill_proc_sync(proc)
        stderr_thread.join(timeout=1.0)
        return {
            "isError": True,
            "message": f"orchestrator error: {type(e).__name__}: {e}",
            "logs": "",
        }


async def _safe_tool_call(
    name: str, args: dict[str, Any], context_snapshot: dict[str, Any] | None
) -> Any:
    """异步包装 registry.execute，供 run_coroutine_threadsafe 调度。

    contextvars 不跨线程传播：本函数在调度时显式 restore 主进程捕获的
    tool_context 快照（user_id/tenant_id/session_id），否则子进程代理调用的
    工具会看到空上下文（workspace_dir 解析到 default/anonymous）。
    """
    if context_snapshot is not None:
        from app.tools.context import restore_context

        restore_context(context_snapshot)
    return await registry.execute(name, args)


async def _run_in_subprocess(
    code: str, tool_names: list[str], timeout: int
) -> dict[str, Any] | None:
    """在 executor 线程中运行同步子进程执行器，避免阻塞主 asyncio loop。

    工具调用通过 ``asyncio.run_coroutine_threadsafe`` 回调主 loop 执行。
    主 loop 的 contextvar 快照在调度前捕获，传入 _safe_tool_call restore。
    """
    from app.tools.context import get_all

    loop = asyncio.get_event_loop()
    # 在主 loop 当前 task 的 contextvar 中捕获快照（测试或 agent runtime 设置的 user_id 等）
    context_snapshot = get_all()
    return await loop.run_in_executor(
        None,
        _run_subprocess_sync,
        code,
        tool_names,
        timeout,
        loop,
        context_snapshot,
    )


def _kill_proc_sync(proc: subprocess.Popen[str]) -> None:
    """SIGKILL 子进程。"""
    try:
        proc.kill()
    except (ProcessLookupError, OSError):
        pass


# 复用线程池，避免每次 _readline_with_deadline 创建新线程
_READER_POOL: concurrent.futures.ThreadPoolExecutor | None = None


def _get_reader_pool() -> concurrent.futures.ThreadPoolExecutor:
    global _READER_POOL
    if _READER_POOL is None:
        _READER_POOL = concurrent.futures.ThreadPoolExecutor(
            max_workers=4,  # P0 性能修复：提升到 4 以避免高并发序列化瓶颈
            thread_name_prefix="subproc_reader",
        )
    return _READER_POOL


def _readline_with_deadline(
    proc: subprocess.Popen[str], deadline: float
) -> str | None:
    """读一行 stdout，带 deadline 超时。

    返回 None 表示超时；返回空串表示 EOF；否则返回一行（含末尾 \\n）。
    使用线程池线程读，避免 readline 在子进程无输出时永久阻塞。
    """
    pool = _get_reader_pool()
    # 调用方都用 Popen(stdout=PIPE)，这里收窄类型（也顺带把"没有 stdout"当作无输出，
    # 与"超时"同样返回 None）。收窄后线程池的提交才有明确签名。
    stdout = proc.stdout
    if stdout is None:  # pragma: no cover - 调用方都传 PIPE
        return None
    fut = pool.submit(stdout.readline)
    remaining = deadline - time.time()
    if remaining <= 0:
        return None
    try:
        return cast("str | None", fut.result(timeout=max(0.001, remaining)))
    except concurrent.futures.TimeoutError:
        # 超时：杀掉子进程让 reader 收尾，然后**必须返回 None（超时）**。
        # 若把 reader 因 EOF 得到的 ""透传出去，主循环会走「subprocess exited
        # unexpectedly」分支，把「超时」误报成「异常退出」。
        _kill_proc_sync(proc)
        try:
            fut.result(timeout=2.0)
        except (concurrent.futures.TimeoutError, Exception):
            pass
        return None
    except Exception:
        return ""


def _wait_proc_sync(proc: subprocess.Popen[str], timeout: float = 2.0) -> None:
    """best-effort 等待子进程退出。"""
    try:
        proc.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        _kill_proc_sync(proc)


# ── 主入口（仅 subprocess 隔离执行，子进程不可用即 fail-closed） ──────


async def run_code(
    code: str, description: str = "", timeout: int = DEFAULT_TIMEOUT_SECONDS
) -> dict[str, Any]:
    """Execute a Python async program against the tool SDK.

    在独立子进程中执行（OS 级隔离 + IPC 代理 tools 调用）；子进程不可用时
    fail-closed 返回错误，绝不在宿主进程直接 exec 用户代码（S 安全修复）。

    Parameters
    ----------
    code:
        Body of ``async def _main():``. May use the injected ``tools``
        namespace and ``return`` a JSON-serializable value.
    description:
        Short summary of what the program does (for the model's own bookkeeping).
    """
    if not code.strip():
        return {"error": "code is required"}
    timeout = max(1, min(timeout, 300))

    tool_names = registry.list_names()

    # S 安全修复：必须走 subprocess 隔离执行,不再降级到主进程 exec 用户代码。
    # 同进程模式无 OS 级隔离,`__class__.__subclasses__()` 等逃逸可达主进程
    # 全部特权 → RCE。子进程不可用即 fail-closed,返回结构化错误而非提升特权执行。
    result = await _run_in_subprocess(code, tool_names, timeout)
    if result is not None:
        return result
    return {
        "isError": True,
        "message": "run_code sandbox subprocess unavailable; refusing to run in host process",
        "logs": "",
    }


# ── Register ─────────────────────────────────────────────────────
registry.register(
    name="run_code",
    description=(
        "Write a Python async program that composes multiple tool calls into one step. "
        "The program body runs as `async def _main()`, with an injected `tools` namespace: "
        "`await tools.<name>(**args)` calls any registered tool and returns its JSON result; "
        "tool failures raise ToolCallError. Capture stdout goes to logs; `return` a "
        "JSON-serializable value as the result. Use this to batch several file/shell/search "
        "operations into a single round trip.\n\n" + sdk_usage_text()
    ),
    parameters={
        "type": "object",
        "properties": {
            "code": {
                "type": "string",
                "description": "Body of `async def _main():` — Python code using the tools namespace",
            },
            "description": {
                "type": "string",
                "description": "Short summary of what the program does",
            },
            "timeout": {
                "type": "integer",
                "description": "Timeout in seconds (default 60, max 300)",
                "default": 60,
            },
        },
        "required": ["code"],
    },
    handler=run_code,
)
