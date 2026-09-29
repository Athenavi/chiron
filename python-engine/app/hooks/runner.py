"""批 G：单个 hook 的沙箱执行（方案 03 §3.2 —— 复用 `plugin_runner.py` 的既有模式）。

**不新造沙箱**：与 `app/api/plugins.py:run_plugin_in_sandbox` 同一套隔离 ——
独立 subprocess（`sys.executable plugin_runner.py`）+ 子进程内 `setrlimit`
内存/CPU 限制 + `app/tools/code_guard.py` 的静态 AST 与运行时受控 builtins；
payload 走 stdin、结果走 stdout，宿主 env 由 `sandboxed_env()` 清空。
与插件链路的唯一差别是**用途**：这里跑的是生命周期 hook，不是插件。

契约（继承自 `plugin_runner.py`）：hook 脚本导出 `main(input) -> dict`；任何
失败都以 `{"success": false, "error": ...}` JSON 输出。本模块把这一切**收束成
永不抛异常**的 `HookOutcome`，让调用方（主流程）天然 fire-and-forget。
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from app.tools.sandbox import sandboxed_env

#: hook 子进程资源上限（hook 只做判定/通知，取与插件沙箱同量级即可）。
_HOOK_MAX_MEMORY_MB = 128
_HOOK_MAX_CPU_SECONDS = 5.0

#: stdout 体积上限，防 hook 刷屏撑爆宿主内存。
_MAX_OUTPUT_BYTES = 262_144  # 256 KiB

#: python-engine 根目录下的 `plugin_runner.py`（复用同一 runner，不复制一份）。
_RUNNER_PATH = Path(__file__).resolve().parents[2] / "plugin_runner.py"


@dataclass(frozen=True)
class HookOutcome:
    """一次 hook 执行的结果（供调用方判定阻断与落审计）。"""

    success: bool
    output: Any
    error: str | None
    exit_code: int | None
    duration_ms: int
    timed_out: bool


def _elapsed_ms(started: float) -> int:
    return int((time.time() - started) * 1000)


async def _terminate(proc: asyncio.subprocess.Process | None) -> None:
    """超时兜底 —— 与 `plugin_runner` 的既有降级语义一致（kill + 回收）。"""
    if proc is None:
        return
    with contextlib.suppress(ProcessLookupError):
        proc.kill()
    with contextlib.suppress(Exception):
        await proc.wait()


async def run_hook(
    *, name: str, code: str, context: dict[str, Any], timeout: float
) -> HookOutcome:
    """在独立子进程中执行一个 hook，返回结构化结果（**永不**向调用方抛异常）。"""
    payload = {
        "plugin_name": name,
        "code": code,
        "input": context,
        "max_memory_mb": _HOOK_MAX_MEMORY_MB,
        "max_cpu_seconds": _HOOK_MAX_CPU_SECONDS,
    }
    started = time.time()
    proc: asyncio.subprocess.Process | None = None
    try:
        proc = await asyncio.create_subprocess_exec(
            sys.executable,
            str(_RUNNER_PATH),
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=str(_RUNNER_PATH.parent),
            env=sandboxed_env(),
        )
        stdout, stderr = await asyncio.wait_for(
            proc.communicate(json.dumps(payload, ensure_ascii=False).encode("utf-8")),
            timeout=timeout,
        )
    except TimeoutError:
        await _terminate(proc)
        return HookOutcome(
            success=False,
            output=None,
            error=f"hook timed out after {timeout}s",
            exit_code=None,
            duration_ms=_elapsed_ms(started),
            timed_out=True,
        )
    except OSError as exc:
        return HookOutcome(
            success=False,
            output=None,
            error=f"failed to start hook sandbox: {exc}",
            exit_code=None,
            duration_ms=_elapsed_ms(started),
            timed_out=False,
        )

    exit_code = proc.returncode
    if len(stdout) > _MAX_OUTPUT_BYTES:
        return HookOutcome(
            success=False,
            output=None,
            error=f"hook output exceeds limit ({len(stdout)} > {_MAX_OUTPUT_BYTES} bytes)",
            exit_code=exit_code,
            duration_ms=_elapsed_ms(started),
            timed_out=False,
        )

    try:
        parsed = json.loads(stdout.decode("utf-8", errors="replace"))
    except json.JSONDecodeError:
        # runner 崩溃（未输出 JSON）：fail-soft —— 记 stderr 供排查，不拖垮主流程。
        tail = stderr.decode("utf-8", errors="replace")[-1000:] if stderr else "no stderr"
        return HookOutcome(
            success=False,
            output=None,
            error=f"hook sandbox crashed (exit={exit_code}): {tail}",
            exit_code=exit_code,
            duration_ms=_elapsed_ms(started),
            timed_out=False,
        )

    return HookOutcome(
        success=bool(parsed.get("success")),
        output=parsed.get("output"),
        error=parsed.get("error"),
        exit_code=exit_code,
        duration_ms=_elapsed_ms(started),
        timed_out=False,
    )
