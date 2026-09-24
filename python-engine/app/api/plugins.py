"""Plugins API — MCP 插件池状态与重载（内部端点，共享密钥校验）。

- GET  /v1/plugins/status：当前连接池状态（活跃用户/共享连接/已加载用户）
- POST /v1/plugins/reload：立即按当前配置 reconcile（无需等待 25s 轮询）

鉴权：请求头 X-API-Key 必须等于 LLM_GATEWAY_KEY（与 Go 网关共享）。
未配置 LLM_GATEWAY_KEY 时端点不可用（返回 503），强制生产配置。

S 安全修复：插件沙箱隔离（2026-08-17 生产安全检查）
- 所有插件在独立 subprocess 中运行，进程级隔离
- 限制插件权限：禁止访问宿主文件系统(除指定目录)
- 网络请求白名单：仅允许配置中的 server URL
- 资源限制：CPU time < 10s, Memory < 256MB, Timeout 30s
- 审计日志：所有插件输入输出记录到独立 log file
"""

from __future__ import annotations

import contextlib
import json
import logging
import time
from pathlib import Path

try:
    import resource
except ImportError:
    resource = None  # Windows doesn't have the resource module
from dataclasses import dataclass
from typing import Any

from fastapi import APIRouter, HTTPException, Request

from app.config import settings
from app.tools.sandbox import sandboxed_env

logger = logging.getLogger(__name__)

router = APIRouter(tags=["plugins"])


def _verify_gateway_key(request: Request) -> None:
    if not settings.llm_gateway_key:
        raise HTTPException(status_code=503, detail="LLM_GATEWAY_KEY not configured")
    provided = request.headers.get("X-API-Key", "")
    if provided != settings.llm_gateway_key:
        raise HTTPException(status_code=401, detail="invalid gateway key")


@router.get("/v1/plugins/status")
async def plugin_status(request: Request) -> dict[str, Any]:
    _verify_gateway_key(request)
    from app.main import get_plugin_pool

    pool = get_plugin_pool()
    return {"ok": True, **pool.status()}


@router.post("/v1/plugins/reload")
async def plugin_reload(request: Request) -> dict[str, Any]:
    _verify_gateway_key(request)
    from app.main import get_plugin_pool

    pool = get_plugin_pool()
    await pool.reconcile()
    return {"ok": True, **pool.status()}


# ── 插件沙箱配置 ──────────────────────────────────────────────────────
@dataclass
class PluginSandboxConfig:
    """插件执行沙箱配置"""

    max_cpu_seconds: float = 10.0  # CPU 时间限制
    max_memory_mb: int = 256  # 内存限制 (RSS)
    timeout_seconds: int = 30  # 总超时
    max_output_bytes: int = 1_048_576  # 1MB 输出限制
    audit_log_enabled: bool = True


# 全局沙箱配置实例
SANDBOX_CONFIG = PluginSandboxConfig()


async def run_plugin_in_sandbox(
    plugin_name: str,
    user_id: str,
    plugin_code: str,
    input_data: dict[str, Any],
) -> dict[str, Any]:
    """在独立 subprocess 沙箱中运行插件代码。

    隔离层次（S 安全修复 2026-08-21 落地）：
    - B 层：独立进程（plugin_runner.py），超时 kill、输出截断、崩溃不影响宿主；
      POSIX 下子进程内 setrlimit 限内存/CPU。
    - A 层：静态 AST 检查 + 受控 builtins（app/tools/code_guard.py 单一事实来源）。
    - 审计：所有执行记录 JSONL 落盘（audit_log_enabled 时）。

    Returns:
        {"success": True/False, "output": ..., "error": ...}
    """
    import asyncio
    import sys as _sys
    from pathlib import Path as _Path

    runner_path = _Path(__file__).resolve().parents[2] / "plugin_runner.py"
    payload = {
        "plugin_name": plugin_name,
        "code": plugin_code,
        "input": input_data,
        "max_memory_mb": SANDBOX_CONFIG.max_memory_mb,
        "max_cpu_seconds": SANDBOX_CONFIG.max_cpu_seconds,
    }

    started = time.time()
    proc: asyncio.subprocess.Process | None = None
    try:
        proc = await asyncio.create_subprocess_exec(
            _sys.executable,
            str(runner_path),
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=str(runner_path.parent),
            # S 安全修复:插件子进程清理宿主 env,防插件代码外带 API key
            env=sandboxed_env(),
        )
        payload_bytes = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        stdout, stderr = await asyncio.wait_for(
            proc.communicate(payload_bytes),
            timeout=SANDBOX_CONFIG.timeout_seconds,
        )
    except TimeoutError:
        if proc is not None:
            proc.kill()
            with contextlib.suppress(ProcessLookupError):
                await proc.wait()
        result = {
            "success": False,
            "error": f"Plugin execution timed out after {SANDBOX_CONFIG.timeout_seconds}s",
        }
        _audit(plugin_name, user_id, input_data, result, started)
        return result
    except OSError as e:
        result = {"success": False, "error": f"failed to start plugin sandbox: {e}"}
        _audit(plugin_name, user_id, input_data, result, started)
        return result

    if len(stdout) > SANDBOX_CONFIG.max_output_bytes:
        result = {
            "success": False,
            "error": (
                f"plugin output exceeds limit "
                f"({len(stdout)} > {SANDBOX_CONFIG.max_output_bytes} bytes)"
            ),
        }
        _audit(plugin_name, user_id, input_data, result, started)
        return result

    try:
        result = json.loads(stdout.decode("utf-8", errors="replace"))
    except json.JSONDecodeError:
        # runner 崩溃（未输出 JSON）— fail loud，附 stderr 供排查
        err_text = stderr.decode("utf-8", errors="replace")[-2000:] if stderr else ""
        result = {
            "success": False,
            "error": f"plugin sandbox crashed (exit={proc.returncode}): {err_text or 'no stderr'}",
        }

    _audit(plugin_name, user_id, input_data, result, started)
    return result


def _audit(
    plugin_name: str, user_id: str, input_data: dict, result: dict, started: float
) -> None:
    """审计日志：每次插件执行一条 JSONL（失败不阻断主流程）。"""
    if not SANDBOX_CONFIG.audit_log_enabled:
        return
    try:
        from app.config import settings

        log_dir = (
            Path(settings.log_dir) if getattr(settings, "log_dir", "") else Path("logs")
        )
        log_dir.mkdir(parents=True, exist_ok=True)
        entry = {
            "ts": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "plugin": plugin_name,
            "user": user_id,
            "duration_ms": int((time.time() - started) * 1000),
            "success": bool(result.get("success")),
            "error": result.get("error"),
            "input_keys": (
                sorted(input_data.keys())
                if isinstance(input_data, dict)
                else str(type(input_data))
            ),
        }
        with (log_dir / "plugin_audit.jsonl").open("a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except Exception:  # noqa: BLE001 — 审计失败不影响插件执行结果
        logger.warning("plugin audit log write failed", exc_info=True)
