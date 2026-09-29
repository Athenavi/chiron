"""经网关 FileStore 的文件后端（批 B 片 4，方案 02 §3.4）。

**解决什么**：agent 的文件工具此前只认本地 `workspace_dir()`，多副本部署下"写 A 副本、
读 B 副本"会间歇性失忆（`app/tools/sandbox.py` 的注释已自陈该风险）。走这里之后，文件落在
部署方配置的 FileStore（local 共享卷 / S3）上，跨副本一致。

**隔离由服务端强制**：引擎只传**相对路径**与身份（租户/用户），存储键由 Go 侧拼成
`agent-files/{tenant}/{user}/{相对路径}` —— 内部端点同样不信任调用方
（见 `internal/api/internal_storage_handler.go`）。

**失败语义**：`base_url` 未配置或网关不可达时，操作返回**结构化错误**而不是空内容 ——
"没有这个文件"与"读不到"必须能区分（方案 02 §3.4 的完成标准之一）。

`stat` / `ls` / `glob` 都基于网关的 `list`（一次拿全树），因此 `glob` 场景下不会
逐文件读全文 —— 那在 100 个文件时就是 100 次跨进程读。
"""

from __future__ import annotations

import base64
import re
from typing import Any

from app.backends.protocol import (
    EditResult,
    FileStat,
    GlobResult,
    GrepMatch,
    GrepResult,
    LsResult,
    ReadBytesResult,
    ReadResult,
    WriteResult,
)


def _glob_to_regex(pattern: str) -> re.Pattern[str]:
    """把 glob 转成正则（支持 `**` 跨目录、`*`/`?`/`[...]`）。

    不能用 `fnmatch`：它把 `*` 当"任意字符（含 `/`）"，于是 `*.py` 会错误匹配
    `sub/dir/a.py`；也不能用 `PurePosixPath.match`：它不支持 `**`。
    """
    out: list[str] = []
    index = 0
    while index < len(pattern):
        char = pattern[index]
        if pattern.startswith("**/", index):
            out.append("(?:.*/)?")
            index += 3
            continue
        if char == "*":
            out.append("[^/]*")
        elif char == "?":
            out.append("[^/]")
        elif char == "[":
            end = pattern.find("]", index + 1)
            if end == -1:
                out.append(re.escape(char))
            else:
                out.append(pattern[index : end + 1])
                index = end + 1
                continue
        else:
            out.append(re.escape(char))
        index += 1
    return re.compile("^" + "".join(out) + "$")


class FileStoreBackend:
    """经网关 `/v1/internal/storage/*` 读写部署级 FileStore。"""

    def __init__(
        self,
        base_url: str,
        internal_token: str,
        *,
        timeout: float = 30.0,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._token = internal_token
        self._timeout = timeout

    # ── 传输层（测试通过 monkeypatch 替换它，不必起真实网关）──

    async def _call(
        self, method: str, path: str, *, params: dict[str, str] | None = None,
        body: dict[str, Any] | None = None,
    ) -> tuple[int, dict[str, Any]]:
        """发一次内部请求，返回 (状态码, JSON)。

        未配置 `base_url` 时返回 503 语义 —— 调用方据此明确失败，而不是读到空内容。
        """
        if not self._base_url:
            return 503, {"error": "file store backend not configured"}

        import httpx

        headers = {"X-Internal-Token": self._token} if self._token else {}
        async with httpx.AsyncClient(
            base_url=self._base_url, timeout=self._timeout, headers=headers
        ) as client:
            resp = await client.request(
                method, path, params=params, json=body
            )
        try:
            payload = resp.json()
        except ValueError:
            payload = {"error": resp.text[:200]}
        return resp.status_code, payload

    # ── 身份（与工具链同源：contextvars）──

    @staticmethod
    def _identity() -> tuple[str, str]:
        from app.tools.context import get_tenant_id, get_user_id

        return get_tenant_id() or "default", get_user_id() or "anonymous"

    async def _list_entries(self, prefix: str = "") -> list[dict[str, Any]]:
        tenant, user = self._identity()
        status, payload = await self._call(
            "GET",
            "/v1/internal/storage/list",
            params={"tenant_id": tenant, "user_id": user, "prefix": prefix},
        )
        if status != 200:
            return []
        files = payload.get("files")
        return [f for f in files if isinstance(f, dict)] if isinstance(files, list) else []

    # ── 协议实现 ──

    async def read(self, path: str, *, offset: int = 0, limit: int = 200) -> ReadResult:
        tenant, user = self._identity()
        status, payload = await self._call(
            "GET",
            "/v1/internal/storage/read",
            params={"tenant_id": tenant, "user_id": user, "path": path},
        )
        if status == 404:
            return ReadResult(path=path, error=f"file not found: {path}")
        if status != 200:
            return ReadResult(
                path=path, error=str(payload.get("error") or f"read failed ({status})")
            )

        text = self._decode(payload)
        lines = text.splitlines()
        if limit <= 0:
            return ReadResult(
                path=path, content=text, total_lines=len(lines), offset=0, limit=0
            )
        page = lines[offset : offset + limit]
        return ReadResult(
            path=path,
            content="\n".join(page),
            total_lines=len(lines),
            offset=offset,
            limit=limit,
            truncated=(offset + limit) < len(lines),
        )

    async def read_bytes(self, path: str) -> ReadBytesResult:
        tenant, user = self._identity()
        status, payload = await self._call(
            "GET",
            "/v1/internal/storage/read",
            params={"tenant_id": tenant, "user_id": user, "path": path},
        )
        if status == 404:
            return ReadBytesResult(path=path, error=f"file not found: {path}")
        if status != 200:
            return ReadBytesResult(
                path=path, error=str(payload.get("error") or f"read failed ({status})")
            )
        if payload.get("encoding") == "base64":
            try:
                return ReadBytesResult(
                    path=path,
                    data=base64.b64decode(str(payload.get("content", ""))),
                )
            except (ValueError, TypeError) as exc:
                return ReadBytesResult(path=path, error=f"invalid base64 payload: {exc}")
        return ReadBytesResult(path=path, data=str(payload.get("content", "")).encode("utf-8"))

    async def stat(self, path: str) -> FileStat | None:
        """经 `list` 取元信息（不读全文 —— glob 会逐文件问它）。"""
        target = path.strip("/")
        for entry in await self._list_entries(prefix=target):
            if str(entry.get("path", "")).strip("/") == target:
                return FileStat(
                    path=path,
                    size=int(entry.get("size") or 0),
                    is_file=not bool(entry.get("is_dir")),
                )
        return None

    async def write(self, path: str, content: str) -> WriteResult:
        tenant, user = self._identity()
        status, payload = await self._call(
            "POST",
            "/v1/internal/storage/write",
            body={"tenant_id": tenant, "user_id": user, "path": path, "content": content},
        )
        if status != 200:
            return WriteResult(
                path=path, error=str(payload.get("error") or f"write failed ({status})")
            )
        return WriteResult(path=path, bytes_written=len(content.encode("utf-8")))

    async def edit(self, path: str, old: str, new: str) -> EditResult:
        """读全文 → 替换 → 写回（FileStore 没有原地编辑能力，这样也便于审计）。"""
        current = await self.read(path, limit=0)
        if current.error:
            return EditResult(path=path, error=current.error)
        occurrences = current.content.count(old)
        if occurrences == 0:
            return EditResult(path=path, error="old text not found")
        written = await self.write(path, current.content.replace(old, new))
        if written.error:
            return EditResult(path=path, error=written.error)
        return EditResult(path=path, occurrences=occurrences)

    async def ls(self, path: str = ".") -> LsResult:
        prefix = "" if path in (".", "./", "") else path.strip("/")
        entries = await self._list_entries(prefix=prefix)
        return LsResult(entries=sorted(str(e.get("path", "")) for e in entries))

    async def glob(self, pattern: str, *, path: str = ".") -> GlobResult:
        prefix = "" if path in (".", "./", "") else path.strip("/")
        entries = await self._list_entries(prefix=prefix)
        regex = _glob_to_regex(pattern)
        paths = [
            str(e.get("path", ""))
            for e in entries
            if not e.get("is_dir") and regex.match(str(e.get("path", "")))
        ]
        return GlobResult(paths=sorted(paths))

    async def grep(
        self, pattern: str, *, path: str = ".", max_results: int = 100
    ) -> GrepResult:
        try:
            regex = re.compile(pattern)
        except re.error as exc:
            return GrepResult(error=f"invalid pattern: {exc}")

        prefix = "" if path in (".", "./", "") else path.strip("/")
        matches: list[GrepMatch] = []
        truncated = False
        for entry in await self._list_entries(prefix=prefix):
            if entry.get("is_dir"):
                continue
            rel = str(entry.get("path", ""))
            read = await self.read(rel, limit=0)
            if read.error:
                continue
            for number, line in enumerate(read.content.splitlines(), start=1):
                if regex.search(line):
                    if len(matches) >= max_results:
                        truncated = True
                        break
                    matches.append(GrepMatch(path=rel, line_number=number, line=line))
            if truncated:
                break
        return GrepResult(
            matches=matches, truncated=truncated, reason="budget" if truncated else None
        )

    def supports_execution(self) -> bool:
        """FileStore 只存文件，不执行命令 —— 调用方据此把执行类工具从工具面剔除。"""
        return False

    # ── 内部 ──

    @staticmethod
    def _decode(payload: dict[str, Any]) -> str:
        if payload.get("encoding") == "base64":
            try:
                return base64.b64decode(str(payload.get("content", ""))).decode(
                    "utf-8", errors="replace"
                )
            except (ValueError, TypeError):
                return ""
        return str(payload.get("content", ""))


def build_filestore_backend_from_settings() -> FileStoreBackend | None:
    """按配置构造 FileStore 后端；未配置网关地址时返回 `None`（保持默认本地后端）。

    开关是 `BACKEND_KIND=filestore`（默认 `local`）—— 显式选择，不做"猜部署形态"那一套
    （与评审 02 §1 的结论一致：判定部署形态不可靠，显式开关才可审计）。
    """
    from app.config import settings

    if (getattr(settings, "backend_kind", "") or "local") != "filestore":
        return None
    base_url = getattr(settings, "gateway_internal_url", "") or ""
    if not base_url:
        return None
    return FileStoreBackend(base_url, getattr(settings, "internal_token", "") or "")
