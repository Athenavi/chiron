"""按前缀路由的复合后端（批 B 片 3，见方案 02 §3.3）。

用途：把"虚拟路径空间"映射到不同后端 —— 例如

```python
CompositeBackend(
    default=LocalWorkspaceBackend(),          # 工作区文件
    routes={"/artifacts/": ArtifactBackend()},# 大工具结果 / checkpoint 快照
)
```

之后 `/artifacts/large_tool_results/xyz` 的读写会落到 artifacts 后端，而 `a.txt` 仍走默认后端。
方案 01 的 C1（checkpoint 快照）与 C2（媒体/大结果卸载）就是要挂到 `/artifacts/` 上。

**路由规则**：按**最长前缀**匹配（`/artifacts/sub/` 优先于 `/artifacts/`），剥离前缀后交给
挂载的后端；挂载点与传入路径都做归一化（`artifacts` / `/artifacts` / `/artifacts/` 等价）。

**一处刻意的取舍**：返回结果里的 `path` **原样透传**（不把虚拟前缀拼回）。因为 Chiron 的
本地后端返回的是**真实绝对路径**，工具会把它展示给模型与用户 —— 拼回虚拟前缀会让"文件到底
在哪"变得不可诊断。deepagents 的 Composite 会拼回，是因为它的后端本来就工作在虚拟路径上；
两者的契约不同，这里以"可诊断"为准。
"""

from __future__ import annotations

from app.backends.protocol import (
    BackendProtocol,
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


def _normalize_mount(mount: str) -> str:
    """把挂载点归一化为 `/<name>/` 形式（`/` 保持为 `/`）。"""
    trimmed = mount.strip().strip("/")
    return "/" if not trimmed else f"/{trimmed}/"


class CompositeBackend:
    """按最长前缀把虚拟路径路由到不同后端。"""

    def __init__(
        self,
        default: BackendProtocol,
        routes: dict[str, BackendProtocol] | None = None,
    ) -> None:
        self._default = default
        normalized = {_normalize_mount(k): v for k, v in (routes or {}).items()}
        # 最长前缀优先：`/artifacts/sub/` 必须赢过 `/artifacts/`
        self._ordered: list[tuple[str, BackendProtocol]] = sorted(
            normalized.items(), key=lambda item: -len(item[0])
        )

    @property
    def mounts(self) -> list[str]:
        """已挂载的前缀（按匹配优先级排列，供诊断/测试）。"""
        return [mount for mount, _ in self._ordered]

    def _resolve(self, path: str) -> tuple[BackendProtocol, str]:
        """返回 (负责该路径的后端, 剥掉前缀后的路径)。"""
        normalized = path if path.startswith("/") else f"/{path}"
        for mount, backend in self._ordered:
            if normalized.startswith(mount):
                return backend, normalized[len(mount) :]
        return self._default, path

    async def read(self, path: str, *, offset: int = 0, limit: int = 200) -> ReadResult:
        backend, inner = self._resolve(path)
        return await backend.read(inner, offset=offset, limit=limit)

    async def read_bytes(self, path: str) -> ReadBytesResult:
        backend, inner = self._resolve(path)
        return await backend.read_bytes(inner)

    async def stat(self, path: str) -> FileStat | None:
        backend, inner = self._resolve(path)
        return await backend.stat(inner)

    async def write(self, path: str, content: str) -> WriteResult:
        backend, inner = self._resolve(path)
        return await backend.write(inner, content)

    async def edit(self, path: str, old: str, new: str) -> EditResult:
        backend, inner = self._resolve(path)
        return await backend.edit(inner, old, new)

    async def ls(self, path: str = ".") -> LsResult:
        backend, inner = self._resolve(path)
        return await backend.ls(inner)

    async def glob(self, pattern: str, *, path: str = ".") -> GlobResult:
        backend, inner = self._resolve(path)
        return await backend.glob(pattern, path=inner)

    async def grep(
        self, pattern: str, *, path: str = ".", max_results: int = 100
    ) -> GrepResult:
        backend, inner = self._resolve(path)
        return await backend.grep(pattern, path=inner, max_results=max_results)

    def supports_execution(self) -> bool:
        """执行能力**只**看默认后端 —— 挂载的路由是存储，不参与执行。"""
        return self._default.supports_execution()

    async def execute(self, command: str, *, timeout: float | None = None) -> ExecuteResult:
        """执行只代理默认后端，且要求它真的支持执行。

        对位 deepagents 的同名约束：`execute` 与 `read`/`write` 必须落在**同一文件系统**上，
        否则"命令写出的文件"与"工具读到的文件"会分叉。所以这里宁可明确报错，也不静默挑一个后端。
        """
        if not self._default.supports_execution():
            return ExecuteResult(
                error="default backend does not support execution",
                exit_code=None,
            )
        if not isinstance(self._default, ExecutingBackendProtocol):
            return ExecuteResult(
                error="default backend claims execution support but lacks execute()",
                exit_code=None,
            )
        return await self._default.execute(command, timeout=timeout)
