"""远程执行后端（方案 03 S5-(e)）：把 `execute` 转发到**独立的 sandbox 服务**。

## 它做什么 / 不做什么

**做**：把"执行"从引擎进程里挪出去 —— **故障隔离**（执行把引擎拖垮）、**资源隔离**（执行能力独立
扩缩/限流）、**审计单点**（执行日志不与引擎日志混）。

**不做**：它**不是安全等级的跃升**。基线（命令白名单 / RLIMIT / AST 守卫）与本地路径**同源**
（服务侧直接复用 `app/tools/sandbox.py` 的同一份实现，而不是重写一份"看起来一样"的名单），
并且仍然**没有**命名空间 / seccomp —— 那些要靠 (c)（远程 provider），已被排除。
别把它读成"做了 (e) 就安全了"。

## 为什么是"包一层"而不是"再写一个 backend"

文件操作（`read` / `write` / `ls` / `glob` / `grep` …）与执行无关。复制一份就等于把 12 个方法连同
它们的语义再维护一遍，而真正需要分流的**只有 `execute`**。所以这里只覆写 `execute`，
**其余全部委托**给 `inner` —— 委托是显式列出的（而不是 `__getattr__` 魔法），这样类型检查与
"这个后端到底实现了什么"都一眼可见。

## 服务不可用时：**失败，不回退**（这是显式选择）

见 `vendor/方案04.md` §4 与设计稿 §7。静默回退本地基线会让"隔离已生效"这个判断**失真** ——
你以为租户走的是隔离服务，实际悄悄跑在引擎进程里，而且**不会有人知道**。

要退回本地，那是**配置层面**的决定（`SANDBOX_BACKEND=local`，或把租户移出白名单），
而不是让运行时偷偷降级。
"""

from __future__ import annotations

import logging
from collections.abc import Sequence

import httpx

from app.backends.protocol import (
    EditResult,
    ExecuteResult,
    ExecutingBackendProtocol,
    FileStat,
    GlobResult,
    GrepResult,
    LsResult,
    ReadBytesResult,
    ReadResult,
    WriteResult,
)
from app.tools.context import get_session_id, get_tenant_id, get_user_id

logger = logging.getLogger(__name__)

#: 与既有 `/v1/internal/*` 同构（设计稿 §2）：形状不另发明一套。
SERVICE_RUN_PATH = "/v1/internal/exec/run"
SERVICE_HEALTH_PATH = "/v1/internal/exec/health"

#: 内部服务调用的默认超时。比"本地执行"长一点：多了一跳网络，且服务侧还有自己的 RLIMIT。
DEFAULT_SERVICE_TIMEOUT_SECONDS = 60.0


class RemoteExecBackend:
    """把 `execute` 转给独立 sandbox 服务；其余方法原样委托 `inner`。

    分流条件（**同时**满足才走服务）：

    1. `url` 非空；
    2. 当前租户在 `tenants` 白名单里。

    空白名单 ⇒ **谁都不走服务** —— 这是刻意的安全默认（不是"没配就全走服务"）。
    """

    def __init__(
        self,
        inner: ExecutingBackendProtocol,
        *,
        url: str = "",
        token: str = "",
        tenants: Sequence[str] = (),
        timeout: float = DEFAULT_SERVICE_TIMEOUT_SECONDS,
    ) -> None:
        self._inner = inner
        self._url = (url or "").strip().rstrip("/")
        self._token = token or ""
        self._tenants = frozenset(t.strip() for t in tenants if t and t.strip())
        self._timeout = float(timeout)

    # ── 分流判定 ──

    def should_use_service(self, *, tenant: str | None = None) -> bool:
        """本次执行该不该走服务。`tenant` 显式传入便于测试；缺省取当前上下文租户。"""
        if not self._url:
            return False
        who = (tenant if tenant is not None else get_tenant_id()) or ""
        return bool(who) and who in self._tenants

    # ── 唯一覆写的方法 ──

    async def execute(self, command: str, *, timeout: float | None = None) -> ExecuteResult:
        if not self.should_use_service():
            return await self._inner.execute(command, timeout=timeout)
        return await self._call_service(command, timeout=timeout)

    async def _call_service(self, command: str, *, timeout: float | None = None) -> ExecuteResult:
        """调服务执行。**任何失败都不回退** —— 回退会让"隔离已生效"失真（见模块文档）。"""
        payload = {
            "command": command,
            # 身份**必须**随请求带上：服务侧据此恢复工具上下文，`run_in_sandbox` 的审计
            # 才答得出"谁在哪个租户/会话里执行的"。不带 ⇒ 流水里三个字段全空，
            # 而这正是把执行挪出去之后最需要保留的信息（隔离不能以"看不见"为代价）。
            "tenant_id": get_tenant_id() or "",
            "user_id": get_user_id() or "",
            "session_id": get_session_id() or "",
        }
        # 服务侧的超时也由它自己兜（它有自己的 RLIMIT）；这里只是网络层上限
        wait = float(timeout) if timeout else self._timeout
        try:
            async with httpx.AsyncClient(timeout=wait) as client:
                resp = await client.post(
                    f"{self._url}{SERVICE_RUN_PATH}",
                    json=payload,
                    headers={"X-Internal-Token": self._token},
                )
        except Exception as exc:  # noqa: BLE001 — 连接类失败一律显式失败
            return self._unavailable(f"{type(exc).__name__}: {exc}")

        if resp.status_code != httpx.codes.OK:
            return self._unavailable(f"HTTP {resp.status_code}: {resp.text[:200]}")

        try:
            data = resp.json()
        except Exception as exc:  # noqa: BLE001
            return self._unavailable(f"invalid response body: {exc}")

        if not isinstance(data, dict):
            return self._unavailable("invalid response shape")

        output = f"{data.get('stdout', '')}{data.get('stderr', '')}"
        exit_code = data.get("exit_code")
        return ExecuteResult(
            output=output,
            exit_code=exit_code if isinstance(exit_code, int) else None,
            truncated=bool(data.get("truncated")),
        )

    @staticmethod
    def _unavailable(detail: str) -> ExecuteResult:
        """服务不可用时的**明确失败**（而不是静默降级到本地）。

        文案要让读到它的人**知道该怎么办** —— 否则只会看到"命令跑不了"。
        """
        message = (
            f"sandbox service unavailable ({detail}); "
            "refusing to fall back to in-process execution "
            "(set SANDBOX_BACKEND=local to opt out explicitly)"
        )
        logger.error("remote exec: %s", message)
        return ExecuteResult(output=message, error=message)

    # ── 其余方法：全部委托（显式列出，便于类型检查与审阅）──

    async def read(self, path: str, *, offset: int = 0, limit: int = 200) -> ReadResult:
        return await self._inner.read(path, offset=offset, limit=limit)

    async def read_bytes(self, path: str) -> ReadBytesResult:
        return await self._inner.read_bytes(path)

    async def stat(self, path: str) -> FileStat | None:
        return await self._inner.stat(path)

    async def write(self, path: str, content: str) -> WriteResult:
        return await self._inner.write(path, content)

    async def edit(self, path: str, old: str, new: str) -> EditResult:
        return await self._inner.edit(path, old, new)

    async def ls(self, path: str = ".") -> LsResult:
        return await self._inner.ls(path)

    async def glob(self, pattern: str, *, path: str = ".") -> GlobResult:
        return await self._inner.glob(pattern, path=path)

    async def grep(
        self, pattern: str, *, path: str = ".", max_results: int = 100
    ) -> GrepResult:
        return await self._inner.grep(pattern, path=path, max_results=max_results)

    def supports_execution(self) -> bool:
        """沿用内层的判断：它不能执行时我们也无法替代（服务只负责"执行"，文件面仍是内层）。"""
        return bool(self._inner.supports_execution())


__all__ = [
    "DEFAULT_SERVICE_TIMEOUT_SECONDS",
    "SERVICE_HEALTH_PATH",
    "SERVICE_RUN_PATH",
    "RemoteExecBackend",
]
