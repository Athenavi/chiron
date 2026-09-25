"""persistent_shell 工具 — 持久终端会话（对应 deepseek-harness tool-bash-persistent）

每个 session 一个长驻 shell 进程：cwd、导出变量、激活环境、函数、后台任务
跨调用持久。

实现要点：非交互 shell 经管道 stdout 是全缓冲（4KB，readline 收不到小输出，
deepseek 用 PTY 解决），故命令输出重定向到临时文件、用哨兵文件标记完成，
规避缓冲问题且保持 shell 状态持久。超时/崩溃时关闭 shell，下次调用重建
（与 deepseek 的 reset 语义一致）。
"""

from __future__ import annotations

import asyncio
import logging
import os
import sys
import tempfile
from pathlib import Path
from typing import Any

from app.tools.context import get_session_id
from app.tools.registry import registry
from app.tools.sandbox import sandboxed_env, workspace_dir

logger = logging.getLogger(__name__)

DEFAULT_TIMEOUT_SECONDS = 120


def _shell_command() -> str:
    if sys.platform == "win32":
        return os.environ.get("COMSPEC", "cmd.exe")
    return os.environ.get("SHELL", "/bin/bash")


class PersistentTerminal:
    """进程内持久 shell 管理器，按 session key 隔离。"""

    def __init__(self) -> None:
        self._procs: dict[str, asyncio.subprocess.Process] = {}

    def _prune_procs(self) -> None:
        """回收已退出的进程条目，防止 _procs 无限累积（S 资源修复）。"""
        dead = [
            k
            for k, p in self._procs.items()
            if p is not None and p.returncode is not None
        ]
        for k in dead:
            self._procs.pop(k, None)

    async def _get_proc(self, key: str) -> asyncio.subprocess.Process:
        proc = self._procs.get(key)
        if proc is not None and proc.returncode is None:
            return proc
        # 回收已退出进程，再创建新的（避免累积完成/崩溃的 shell）
        self._prune_procs()
        # S 修复：ws 用局部变量，避免写成实例属性后被并发请求覆盖导致跨会话串 cwd/cd
        ws = str(workspace_dir())
        shell_exe = _shell_command()
        proc = await asyncio.create_subprocess_exec(
            shell_exe,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
            cwd=ws,
            # S 安全修复：清理宿主 env(API key/JWT_SECRET/internal token 等)，
            # 仅保留 PATH/HOME 等基础变量并重定向到沙箱，防止模型 `env` 外带密钥。
            env=sandboxed_env(),
        )
        # 首命令 cd 到沙箱 workspace，保证状态持久在隔离目录内（S 安全修复）
        # stdin 显式用了 PIPE，这里只是把类型收窄（Popen 的返回类型是 StreamWriter | None）。
        assert proc.stdin is not None
        try:
            if sys.platform == "win32":
                proc.stdin.write(f'cd /d "{ws}"\n'.encode())
            else:
                proc.stdin.write(f'cd "{ws}"\n'.encode())
            await proc.stdin.drain()
        except Exception:
            pass
        self._procs[key] = proc
        return proc

    async def execute(
        self, key: str, command: str, timeout: int = DEFAULT_TIMEOUT_SECONDS
    ) -> dict[str, Any]:
        proc = await self._get_proc(key)

        # 逃逸拦截：与 shell_exec 同一套规则（绝对路径/父目录/云元数据），
        # 否则持久 shell 可直接 cat /etc/passwd 等（S 安全修复）
        from app.tools.sandbox import _has_escape

        esc = _has_escape(command)
        if esc:
            return {
                "output": f"[blocked: {esc}] command not allowed in sandbox",
                "exit_code": -1,
                "persistent": True,
            }

        # 每命令一个临时工作目录：out=输出 / code=退出码 / done=完成哨兵
        workdir = Path(tempfile.mkdtemp(prefix="chiron_sh_"))
        out_file = workdir / "out.txt"
        code_file = workdir / "code.txt"
        done_file = workdir / "done.txt"

        if sys.platform == "win32":
            lines = (
                f'{command} > "{out_file}" 2>&1',
                f'echo %errorlevel% > "{code_file}"',
                f'type nul > "{done_file}"',
            )
        else:
            lines = (
                f'{command} > "{out_file}" 2>&1',
                f'echo $? > "{code_file}"',
                f'touch "{done_file}"',
            )

        # stdin 显式用了 PIPE，这里只是把类型收窄。
        assert proc.stdin is not None
        try:
            proc.stdin.write("\n".join(lines).encode("utf-8") + b"\n")
            await proc.stdin.drain()
        except (BrokenPipeError, ConnectionResetError, ValueError):
            await self._discard(key)
            return {
                "output": "[shell exited — reset]",
                "exit_code": -1,
                "persistent": False,
                "reset": True,
            }

        # 轮询完成哨兵
        elapsed = 0.0
        while elapsed < timeout:
            if done_file.exists():
                break
            if proc.returncode is not None:
                await self._discard(key)
                return {
                    "output": f"[shell exited: code {proc.returncode} — reset]",
                    "exit_code": proc.returncode or 0,
                    "persistent": False,
                    "reset": True,
                }
            await asyncio.sleep(0.05)
            elapsed += 0.05
        else:
            # 超时：关闭不确定的 shell，下次重建
            await self._discard(key)
            partial = (
                out_file.read_text(encoding="utf-8", errors="replace")
                if out_file.exists()
                else ""
            )
            return {
                "output": (partial + "\n" if partial else "")
                + f"[timed out after {timeout}s — shell reset]",
                "exit_code": -1,
                "persistent": False,
                "reset": True,
            }

        output = (
            out_file.read_text(encoding="utf-8", errors="replace")
            if out_file.exists()
            else ""
        )
        exit_code = 0
        try:
            exit_code = int(code_file.read_text(encoding="utf-8").strip() or 0)
        except (OSError, ValueError):
            pass

        # 临时目录清理：防止 /tmp 长期累积（S10 修复）
        try:
            import shutil
            shutil.rmtree(workdir, ignore_errors=True)
        except Exception:
            pass

        return {"output": output, "exit_code": exit_code, "persistent": True}

    async def _discard(self, key: str) -> None:
        proc = self._procs.pop(key, None)
        if proc is not None and proc.returncode is None:
            try:
                proc.kill()
                await proc.wait()
            except Exception:  # noqa: BLE001
                pass

    async def close_all(self) -> None:
        for key in list(self._procs.keys()):
            await self._discard(key)


# 进程内单例
_terminal = PersistentTerminal()


async def persistent_shell(
    command: str, timeout: int = DEFAULT_TIMEOUT_SECONDS
) -> dict[str, Any]:
    """Run *command* in the session's persistent shell.

    cwd, exported variables, activated environments, functions and background
    jobs persist across calls in the same session. A timeout or shell exit
    resets the shell; the next call starts fresh (the result says so).
    """
    if not command.strip():
        return {"error": "command is required"}
    key = get_session_id() or "default"
    return await _terminal.execute(key, command, timeout=timeout)


registry.register(
    name="persistent_shell",
    description=(
        "Run a command in a persistent shell shared across calls in this session. "
        "State (cwd, exports, activated venv, functions, background jobs) persists "
        "between calls — use `cd` once and it stays. Prefer this over shell_exec "
        "for multi-step work. "
        "The command's stdout/stderr are captured and returned as `output`; "
        "do NOT use `>`/`2>` redirection to files inside the command (it is "
        "captured by the tool) — save files with the write_file tool instead. "
        "A timeout or shell crash resets the shell and the next call starts fresh."
    ),
    parameters={
        "type": "object",
        "properties": {
            "command": {"type": "string", "description": "Shell command to run"},
            "timeout": {
                "type": "integer",
                "default": DEFAULT_TIMEOUT_SECONDS,
                "description": "Seconds before the shell is reset",
            },
        },
        "required": ["command"],
    },
    handler=persistent_shell,
)
