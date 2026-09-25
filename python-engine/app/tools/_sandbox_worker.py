"""子进程沙箱执行器 — 与主进程通过 stdin/stdout JSON-line IPC 通信。

协议（每行一个 JSON 对象，UTF-8，无 BOM）：

主进程 → 子进程 stdin：
- ``{"type":"init","code":"...","tools":["name1","name2"]}``  启动信号
- ``{"type":"tool_result","call_id":1,"result":{...}}``         工具调用成功响应
- ``{"type":"tool_error","call_id":1,"error":"..."}``           工具调用失败响应

子进程 → 主进程 stdout：
- ``{"type":"tool_call","call_id":1,"name":"read_file","args":{...}}``
- ``{"type":"done","result":<json>,"logs":"..."}``              程序正常返回
- ``{"type":"error","message":"...","logs":"..."}``             程序异常/守卫拦截

子进程启动顺序：
1. 应用 RLIMIT（POSIX；Windows 跳过）
2. 从 stdin 阻塞读 init 消息
3. 静态守卫检查 code
4. exec code（注入 safe_builtins + ToolProxy 命名空间）
5. asyncio.run(_main()) 执行
6. 输出 done / error 后退出

模型代码在子进程内通过 ``await tools.<name>(**args)`` 触发 IPC 调用主进程的真实工具；
主进程持有 registry 与所有宿主资源（DB/Redis/文件系统），子进程无直接访问权限。
"""

from __future__ import annotations

import asyncio
import contextlib
import io
import json
import logging
import os
import sys
from typing import Any

try:
    import resource as _resource  # POSIX only

    _HAS_RESOURCE = True
except ImportError:
    _HAS_RESOURCE = False

from app.tools.code_guard import check_static as _check_static
from app.tools.code_guard import safe_builtins as _safe_builtins

logger = logging.getLogger(__name__)

# 资源限制（POSIX；Windows 下子进程不应用 rlimit，依赖主进程 wall-clock SIGKILL）
_MEM_LIMIT_BYTES = 512 * 1024 * 1024  # 512MB
_CPU_LIMIT_SECONDS = 30
_FILE_LIMIT_BYTES = 10 * 1024 * 1024  # 10MB
MAX_LOG_CHARS = 20_000


def _apply_rlimit() -> None:
    """在 fork 后立即应用 POSIX 资源限制。失败时降级（不阻断启动）。"""
    if not _HAS_RESOURCE:
        return
    try:
        _resource.setrlimit(_resource.RLIMIT_AS, (_MEM_LIMIT_BYTES, _MEM_LIMIT_BYTES))
        _resource.setrlimit(
            _resource.RLIMIT_CPU, (_CPU_LIMIT_SECONDS, _CPU_LIMIT_SECONDS)
        )
        _resource.setrlimit(
            _resource.RLIMIT_FSIZE, (_FILE_LIMIT_BYTES, _FILE_LIMIT_BYTES)
        )
    except (ValueError, OSError) as e:
        # 沙箱降级：仅记录到 stderr（不写 stdout，避免污染协议）
        sys.stderr.write(f"[sandbox] setrlimit failed (relaxed): {e}\n")


class ToolCallError(Exception):
    """子进程内传播的工具调用失败（与主进程 ToolCallError 语义一致）。"""

    def __init__(self, tool_name: str, message: str):
        self.tool_name = tool_name
        self.message = message
        super().__init__(f"ToolCallError({tool_name}): {message}")


class ToolProxy:
    """通过 stdout/stdin JSON-line 协议代理 tools 调用到主进程。

    子进程内模型代码调用 ``await tools.read_file(path='x')`` 时：
    1. 写 stdout 一行 ``tool_call``
    2. 阻塞读 stdin 等待 ``tool_result`` 或 ``tool_error``
    3. 返回 result 或抛 ToolCallError

    所有调用串行化（asyncio.Lock），避免 stdout 写入交错。
    """

    def __init__(self, names: list[str]):
        self._names = set(names)
        self._call_id = 0
        self._lock = asyncio.Lock()

    async def _call(self, name: str, args: dict[str, Any]) -> Any:
        if name not in self._names:
            raise ToolCallError(name, f"tool '{name}' not registered for this run")

        async with self._lock:
            self._call_id += 1
            call_id = self._call_id

        # 写 stdout（os.write 绕过 stdio 缓冲，立即推送）
        _write_msg(
            {
                "type": "tool_call",
                "call_id": call_id,
                "name": name,
                "args": args,
            }
        )

        # 阻塞读 stdin（async 中用 run_in_executor 避免阻塞事件循环）
        loop = asyncio.get_event_loop()
        try:
            line = await asyncio.wait_for(
                loop.run_in_executor(None, _read_msg),
                timeout=30.0,  # 防止父进程崩溃后子进程无限阻塞
            )
        except TimeoutError:
            raise ToolCallError(
                name, "parent process did not respond within 30s (timeout)"
            ) from None
        if not line:
            raise ToolCallError(name, "parent closed stdin (worker killed?)")

        try:
            msg = json.loads(line)
        except json.JSONDecodeError as e:
            raise ToolCallError(name, f"invalid IPC response: {e}") from e

        mtype = msg.get("type")
        if mtype == "tool_error":
            raise ToolCallError(name, str(msg.get("error", "unknown tool error")))
        if mtype != "tool_result":
            raise ToolCallError(name, f"unexpected IPC message type: {mtype}")
        return msg.get("result")

    def __getattr__(self, name: str) -> Any:
        """动态生成每个注册工具的 async 调用方法。

        兼容两种调用形式：
        - ``await tools.read_file(path='x')`` （kwargs）
        - ``await tools.read_file({'path': 'x'})`` （单 dict 参数，向后兼容）
        """
        if name.startswith("_"):
            raise AttributeError(name)

        async def _wrapped(*args: Any, **kwargs: Any) -> Any:
            if len(args) == 1 and isinstance(args[0], dict) and not kwargs:
                params: dict[str, Any] = args[0]
            else:
                params = kwargs
            return await self._call(name, params)

        return _wrapped


def _write_msg(msg: dict[str, Any]) -> None:
    """写一行 JSON 到 stdout（协议输出）。

    使用 os.write(1, ...) 直接写 fd 1，绕过 stdio 缓冲层；
    Windows 与 POSIX 上一致立即推送，避免 block-buffered pipe。
    """
    data = (json.dumps(msg, ensure_ascii=False, default=str) + "\n").encode("utf-8")
    # 分块写：Windows 上 os.write 可能不一次写完
    written = 0
    while written < len(data):
        n = os.write(1, data[written:])
        if n <= 0:
            break
        written += n


def _read_msg() -> str:
    """读一行 JSON 从 stdin（协议输入）。

    用 sys.stdin.readline 阻塞读；父进程按行写，每次一行。
    """
    return sys.stdin.readline()


def _run_program(code: str, tool_names: list[str]) -> int:
    """执行模型代码并输出 done/error。返回 exit code。"""
    # 静态守卫（禁止 os/subprocess/socket 等危险 import/attr）
    denied = _check_static(code)
    if denied:
        _write_msg(
            {
                "type": "error",
                "message": f"blocked by static guard: {denied}",
                "logs": "",
            }
        )
        return 1

    log_buf = io.StringIO()
    proxy = ToolProxy(tool_names)

    ns: dict[str, Any] = {
        "tools": proxy,
        "ToolCallError": ToolCallError,
        "__builtins__": _safe_builtins(),
    }

    # 包装为 async def _main()
    body = "\n".join(
        "    " + line if line.strip() else line for line in code.splitlines()
    )
    src = f"async def _main():\n{body}\n"

    # 安全假设：
    # - ns["__builtins__"] = _safe_builtins() 提供运行时守卫（危险 builtin stub + 安全 import 白名单）
    # - 静态守卫（BLOCKED_SUBCLASS_ATTRS）封禁 __class__/__subclasses__ 等逃逸链属性访问
    # - safe_builtins() 移除 __builtins__ 键，防止通过 __builtins__["open"] 索引访问原始内置函数
    # 纵深防御：子进程 OS 级隔离（RLIMIT + wall-clock 超时 SIGKILL 兜底）
    try:
        exec(
            compile(src, "<sandbox>", "exec"), ns
        )  # noqa: S102 — 沙箱语义由部署策略约束
        main_fn = ns["_main"]

        async def _run() -> Any:
            with contextlib.redirect_stdout(log_buf):
                result = await main_fn()
            return result

        result = asyncio.run(_run())
        _write_msg(
            {
                "type": "done",
                "result": result,
                "logs": log_buf.getvalue()[-MAX_LOG_CHARS:],
            }
        )
        return 0
    except ToolCallError as e:
        _write_msg(
            {
                "type": "error",
                "message": f"tool call failed: {e.tool_name} — {e.message}",
                "logs": log_buf.getvalue()[-MAX_LOG_CHARS:],
            }
        )
        return 1
    except SyntaxError as e:
        _write_msg(
            {
                "type": "error",
                "message": f"syntax error: {e}",
                "logs": "",
            }
        )
        return 1
    except Exception as e:  # noqa: BLE001 — 模型程序异常按结构化错误返回
        _write_msg(
            {
                "type": "error",
                "message": f"{type(e).__name__}: {e}",
                "logs": log_buf.getvalue()[-MAX_LOG_CHARS:],
            }
        )
        return 1


def main() -> int:
    """子进程入口。"""
    _apply_rlimit()

    # 阻塞读 init 消息
    init_line = sys.stdin.readline()
    if not init_line:
        _write_msg({"type": "error", "message": "no init message", "logs": ""})
        return 1

    try:
        init = json.loads(init_line)
    except json.JSONDecodeError as e:
        _write_msg({"type": "error", "message": f"invalid init: {e}", "logs": ""})
        return 1

    if init.get("type") != "init":
        _write_msg(
            {
                "type": "error",
                "message": f"expected init, got {init.get('type')}",
                "logs": "",
            }
        )
        return 1

    code = init.get("code", "")
    tool_names = init.get("tools", [])
    if not isinstance(tool_names, list):
        tool_names = []

    return _run_program(code, tool_names)


if __name__ == "__main__":
    sys.exit(main())
