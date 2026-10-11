"""Chiron sandbox 服务（S5-(e)）—— 把「执行」从引擎进程里挪出去的**独立服务**。

## 它是什么 / 不是什么

**是**：故障隔离（执行把引擎拖垮）、资源隔离（执行独立扩缩/限流）、审计单点。
**不是**：安全等级的跃升 —— 基线（命令白名单 / RLIMIT / 逃逸拦截 / 审计）**直接复用引擎那一份**
（`app/tools/sandbox.py` 的 `run_in_sandbox`），不重写一份"看起来一样"的名单（两份名单必然漂移，
而且漂移方向通常是**放宽**）。仍然**没有**命名空间 / seccomp。

## 契约

与引擎既有 `/v1/internal/*` **同构**：请求头 `X-Internal-Token` + JSON body。

```
GET  /v1/internal/exec/health   -> {"status": "ok", ...}
POST /v1/internal/exec/run      -> {"stdout","stderr","exit_code","truncated"}
     body: {"command": "...", "tenant_id"?, "user_id"?, "session_id"?}
```

身份字段**可选**：带上它们，服务侧才会把执行归到正确的租户/会话
（`run_in_sandbox` 的审计从工具上下文取身份）；不带就是空身份 —— 那等于"记下了执行、
记不下人"，所以引擎侧客户端应当传（见 `app/backends/remote_exec.py`）。

## 部署边界（服务应用之外的必做项，见 vendor/规划.md §5.3）

无状态 · 最小权限 · **不挂可写宿主路径** · **不与引擎共容器** · 只在内网可达 ·
自己的 `SANDBOX_ROOT`（独立卷）。
"""

from __future__ import annotations

import asyncio
import hmac
import os
import sys
from pathlib import Path
from typing import Any

# 复用引擎的**同一份**基线实现：把 python-engine 放进 import 路径（部署时用 PYTHONPATH 更显式）。
_ENGINE_DIR = Path(__file__).resolve().parent.parent / "python-engine"
if _ENGINE_DIR.is_dir() and str(_ENGINE_DIR) not in sys.path:
    sys.path.insert(0, str(_ENGINE_DIR))

from fastapi import FastAPI, Header, HTTPException  # noqa: E402
from pydantic import BaseModel, Field  # noqa: E402

from app.tools.sandbox import run_in_sandbox  # noqa: E402

#: 内部令牌的环境变量名（与引擎/网关的 `/v1/internal/*` 约定一致）
TOKEN_ENV = "INTERNAL_TOKEN"

#: 单次执行的默认上限（秒）。服务自己的硬上限，不信任调用方传的值。
DEFAULT_TIMEOUT_SECONDS = 120

#: 同时在飞的执行数上限（超出则**排队**，而不是直接失败）。
#: 与容器 `pids_limit` 对齐：一次执行约 2-3 个进程，因此 `pids_limit` 应 ≥ MAX_INFLIGHT × 3。
MAX_INFLIGHT = max(1, int(os.getenv("SANDBOX_MAX_INFLIGHT", "8") or 8))

#: 排队等待上限（秒）。超过即拒绝（非零退出码 + 原因）—— 避免请求无限堆积把内存吃满。
QUEUE_TIMEOUT_SECONDS = float(os.getenv("SANDBOX_QUEUE_TIMEOUT_SECONDS", "30") or 30)

#: 运行计数（单 event loop 下字典操作本身是原子的；仅供 `/stats` 观测）。
_stats: dict[str, int] = {
    "inflight": 0,
    "queued": 0,
    "executed_total": 0,
    "rejected_total": 0,
}

#: 并发闸门。多副本 / 多 worker 部署时**每个 worker 各持一份**（见 docs 的扩缩说明）。
_semaphore = asyncio.Semaphore(MAX_INFLIGHT)

app = FastAPI(title="chiron-sandbox", docs_url=None, redoc_url=None, openapi_url=None)


class RunRequest(BaseModel):
    command: str
    #: 可选身份：带上才谈得上"审计到人"（见模块文档）
    tenant_id: str = ""
    user_id: str = ""
    session_id: str = ""
    timeout_seconds: int = Field(default=DEFAULT_TIMEOUT_SECONDS, ge=1)


def require_token(provided: str) -> None:
    """校验 `X-Internal-Token`：**未配置令牌 = 拒绝服务**（fail-closed，不是放行）。"""
    expected = (os.getenv(TOKEN_ENV) or "").strip()
    if not expected:
        raise HTTPException(status_code=503, detail=f"{TOKEN_ENV} is not configured")
    if not provided or not hmac.compare_digest(provided, expected):
        raise HTTPException(status_code=401, detail="invalid internal token")


def to_client_shape(result: dict[str, Any]) -> dict[str, Any]:
    """把 `run_in_sandbox` 的结果映射成契约形状（客户端只认 stdout/stderr/exit_code/truncated）。

    被拦下/超时在基线里是 `{"error": ...}`（没有 exit_code），这里统一成**非零退出码 +
    原因进 stderr**：调用方（引擎）据此能看到"为什么没跑"，而不是一个空结果。
    """
    if "exit_code" in result:
        return {
            "stdout": result.get("stdout", ""),
            "stderr": result.get("stderr", ""),
            "exit_code": result.get("exit_code"),
            "truncated": bool(result.get("truncated")),
        }
    # ⚠ 先取 `error`（给人看的原因），再退到 `reason` —— 基线的 `reason` 是**命中的正则模式**，
    # 直接透出去会得到 `(^|[^A-Za-z0-9_.])(\.\.)[\\/]` 这种谁读都看不懂的东西（实测）。
    reason = str(result.get("error") or result.get("reason") or "execution failed")
    if result.get("error") == "timeout":
        reason = f"timed out after {result.get('timeout')}s"
    return {"stdout": "", "stderr": reason, "exit_code": -1, "truncated": False}


@app.get("/v1/internal/exec/health")
async def health(x_internal_token: str = Header(default="")) -> dict[str, Any]:
    require_token(x_internal_token)
    return {"status": "ok", "service": "chiron-sandbox"}


@app.get("/v1/internal/exec/stats")
async def stats(x_internal_token: str = Header(default="")) -> dict[str, Any]:
    """并发观测：在飞 / 排队 / 累计执行 / 累计拒绝。

    扩缩与压测的口径来源（见 `scripts/bench_sandbox.py`）：若 `queued` 持续 > 0 或
    `rejected_total` 增长，说明 `MAX_INFLIGHT`（或副本数）不够。
    """
    require_token(x_internal_token)
    return {
        "max_inflight": MAX_INFLIGHT,
        "queue_timeout_seconds": QUEUE_TIMEOUT_SECONDS,
        **_stats,
    }


@app.post("/v1/internal/exec/run")
async def run(
    req: RunRequest, x_internal_token: str = Header(default="")
) -> dict[str, Any]:
    require_token(x_internal_token)

    # 并发闸门：超出 MAX_INFLIGHT 则**排队**；排队超过 QUEUE_TIMEOUT_SECONDS 才拒绝。
    # 突发流量因此表现为延迟而非失败，同时用 rejected_total 给出"该扩容了"的明确信号。
    _stats["queued"] += 1
    try:
        await asyncio.wait_for(_semaphore.acquire(), timeout=QUEUE_TIMEOUT_SECONDS)
    except TimeoutError:
        _stats["queued"] -= 1
        _stats["rejected_total"] += 1
        return {
            "stdout": "",
            "stderr": (
                f"queue timeout after {QUEUE_TIMEOUT_SECONDS:g}s "
                f"(max_inflight={MAX_INFLIGHT}) — 沙箱执行容量已满，请扩容或降低并发"
            ),
            "exit_code": -1,
            "truncated": False,
        }
    _stats["queued"] -= 1
    _stats["inflight"] += 1
    try:
        # 身份恢复：`run_in_sandbox` 的审计从工具上下文取 tenant/user/session，
        # 不恢复就会写出"空身份"的流水（谁执行的答不出来）。
        if req.tenant_id or req.user_id or req.session_id:
            from app.tools.context import set_tool_context

            set_tool_context(
                tenant_id=req.tenant_id, user_id=req.user_id, session_id=req.session_id
            )

        timeout = min(int(req.timeout_seconds or DEFAULT_TIMEOUT_SECONDS), DEFAULT_TIMEOUT_SECONDS)
        result = await run_in_sandbox(req.command, timeout=timeout)
        _stats["executed_total"] += 1
        return to_client_shape(result)
    finally:
        _stats["inflight"] -= 1
        _semaphore.release()
