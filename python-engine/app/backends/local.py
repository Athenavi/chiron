"""本地工作区后端（今天的默认行为，等价实现）。

**这一层刻意不做横切逻辑**：

- read-before-write 观测（`app/tools/fs_guard.py`）
- 写入前 undo 快照（`app/agent/undo_stack.py`）
- 动作分级与审批（`app/agent/tool_policy.py`）

它们留在**工具层**。理由：这样"引入后端抽象"就是纯粹的**内部实现替换** —— 对外返回契约与
安全语义都不动，现有工具测试即成为回归网（批 B 的验收标准就是"零行为漂移"）。

> 片 1 只把 `read` / `write` 接到工具上（`read_file` / `write_file`）；`ls` / `glob` / `grep`
> 的实现在片 2 迁移工具时与现有实现逐条对齐后再启用 —— 先定义协议、后迁移，避免一次改太多。

**并发约束（P1 修复）**：本类是 `async` 契约，但底层 `pathlib` 是**阻塞 I/O**。
此前直接在 event loop 里调用 `read_text` / `write_text` / `rglob` / `stat`，一旦并发
（尤其 `grep` 会遍历整个工作区并逐文件读取）就会冻结该 event loop 上的**所有**请求。
现统一经 `asyncio.to_thread` 下沉到线程池；返回契约与安全语义保持不变。
"""

from __future__ import annotations

import asyncio
import re
from pathlib import Path

from app.backends.protocol import (
    DeleteResult,
    EditResult,
    ExecuteResult,
    FileStat,
    GlobResult,
    GrepMatch,
    GrepResult,
    LsResult,
    ReadBytesResult,
    ReadResult,
    WriteResult,
)
from app.tools.sandbox import safe_join


def _grep_sync(base: Path, regex: re.Pattern[str], max_results: int) -> GrepResult:
    """`grep` 的同步实现（在工作线程中运行）：目录遍历 + 逐文件读取。"""
    candidates = sorted(base.rglob("*")) if base.is_dir() else [base]
    matches: list[GrepMatch] = []
    truncated = False
    for file in candidates:
        if not file.is_file():
            continue
        try:
            text = file.read_text(encoding="utf-8", errors="replace")
        except OSError:  # 不可读文件跳过，不让整次搜索失败
            continue
        for number, line in enumerate(text.splitlines(), start=1):
            if not regex.search(line):
                continue
            if len(matches) >= max_results:
                truncated = True
                break
            matches.append(GrepMatch(path=str(file), line_number=number, line=line))
        if truncated:
            break
    return GrepResult(
        matches=matches, truncated=truncated, reason="budget" if truncated else None
    )


class LocalWorkspaceBackend:
    """`sandbox.safe_join` + 本地文件系统。"""

    async def read(self, path: str, *, offset: int = 0, limit: int = 200) -> ReadResult:
        target = safe_join(path)
        if not await asyncio.to_thread(target.exists):
            return ReadResult(path=str(target), error=f"file not found: {path}")
        text = await asyncio.to_thread(
            target.read_text, encoding="utf-8", errors="replace"
        )
        lines = text.splitlines()
        if limit <= 0:
            # `limit <= 0` = 读**全文原文**（忽略 offset）：`edit_file` 需要逐字节精确的内容
            # 做唯一性校验与 diff —— 若这里做行归一化（`"\n".join(splitlines())`），
            # 原文末尾的换行会丢、CRLF 会被改写，替换语义随之漂移。
            return ReadResult(
                path=str(target),
                content=text,
                total_lines=len(lines),
                offset=0,
                limit=0,
                truncated=False,
            )
        page = lines[offset : offset + limit]
        # 截断由**产生截断的这一层**给出，不由调用方扫文本猜测（协议约束 2）
        truncated = (offset + limit) < len(lines)
        return ReadResult(
            path=str(target),
            content="\n".join(page),
            total_lines=len(lines),
            offset=offset,
            limit=limit,
            truncated=truncated,
        )

    async def read_bytes(self, path: str) -> ReadBytesResult:
        target = safe_join(path)
        if not await asyncio.to_thread(target.exists):
            return ReadBytesResult(path=str(target), error=f"file not found: {path}")
        try:
            data = await asyncio.to_thread(target.read_bytes)
            return ReadBytesResult(path=str(target), data=data)
        except OSError as exc:
            # 目录 / 权限 / 占用：把 OS 错误变成结构化错误，而不是让调用方接异常
            return ReadBytesResult(path=str(target), error=str(exc))

    async def stat(self, path: str) -> FileStat | None:
        target = safe_join(path)
        try:
            info = await asyncio.to_thread(target.stat)
            is_file = await asyncio.to_thread(target.is_file)
        except OSError:
            return None
        return FileStat(path=str(target), size=info.st_size, is_file=is_file)

    async def write(self, path: str, content: str) -> WriteResult:
        target = safe_join(path)
        await asyncio.to_thread(target.parent.mkdir, parents=True, exist_ok=True)
        await asyncio.to_thread(target.write_text, content, encoding="utf-8")
        return WriteResult(path=str(target), bytes_written=len(content.encode("utf-8")))

    async def edit(self, path: str, old: str, new: str) -> EditResult:
        target = safe_join(path)
        if not await asyncio.to_thread(target.exists):
            return EditResult(path=str(target), error=f"file not found: {path}")
        text = await asyncio.to_thread(
            target.read_text, encoding="utf-8", errors="replace"
        )
        occurrences = text.count(old)
        if occurrences == 0:
            return EditResult(path=str(target), error="old text not found")
        await asyncio.to_thread(
            target.write_text, text.replace(old, new), encoding="utf-8"
        )
        return EditResult(path=str(target), occurrences=occurrences)

    async def delete(self, path: str) -> DeleteResult:
        """删除**文件**（不递归删目录 —— 那是更危险的动作，不做隐式扩展）。"""
        target = safe_join(path)
        if not await asyncio.to_thread(target.exists):
            return DeleteResult(path=str(target), deleted=False)
        if await asyncio.to_thread(target.is_dir):
            return DeleteResult(
                path=str(target),
                error="path is a directory; delete_file only removes files",
            )
        try:
            await asyncio.to_thread(target.unlink)
        except OSError as exc:
            # 权限 / 占用：把 OS 错误变成结构化错误，而不是让调用方接异常
            return DeleteResult(path=str(target), error=str(exc))
        return DeleteResult(path=str(target), deleted=True)

    async def ls(self, path: str = ".") -> LsResult:
        target = safe_join(path)
        if not await asyncio.to_thread(target.exists):
            return LsResult(error=f"path not found: {path}")
        if await asyncio.to_thread(target.is_file):
            return LsResult(entries=[str(target)])
        entries = await asyncio.to_thread(
            lambda: sorted(str(p) for p in target.iterdir())
        )
        return LsResult(entries=entries)

    async def glob(self, pattern: str, *, path: str = ".") -> GlobResult:
        base = safe_join(path)
        paths = await asyncio.to_thread(
            lambda: sorted(str(p) for p in base.glob(pattern))
        )
        return GlobResult(paths=paths)

    async def grep(
        self, pattern: str, *, path: str = ".", max_results: int = 100
    ) -> GrepResult:
        base = safe_join(path)
        try:
            regex = re.compile(pattern)
        except re.error as exc:
            return GrepResult(error=f"invalid pattern: {exc}")

        # 目录遍历 + 逐文件读取下沉到线程池：这是本层最重的阻塞点。
        return await asyncio.to_thread(_grep_sync, base, regex, max_results)

    def supports_execution(self) -> bool:
        return True

    async def execute(self, command: str, *, timeout: float | None = None) -> ExecuteResult:
        from app.tools.sandbox import run_in_sandbox

        result = await run_in_sandbox(command, timeout=int(timeout) if timeout else 120)
        if "error" in result:
            return ExecuteResult(output=str(result["error"]), error=str(result["error"]))
        output = f"{result.get('stdout', '')}{result.get('stderr', '')}"
        exit_code = result.get("exit_code")
        # 片 5：`run_in_sandbox` 已按上限截断，这里把"是否截断"如实带出来 ——
        # 让上层不必扫文本猜测（协议约束 2）
        return ExecuteResult(
            output=output,
            exit_code=exit_code if isinstance(exit_code, int) else None,
            truncated=bool(result.get("truncated")),
        )
