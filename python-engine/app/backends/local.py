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
import os
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


#: 判二进制时探测的头部字节数（与 Reasonix 的 `readFileBinaryPeek` 同值）。
_BINARY_PEEK = 8 * 1024
#: UTF-16 的每个 ASCII 字符都含 0x00 —— 带 BOM 的 UTF-16 不是二进制（Reasonix 同款教训：
#: 先查 BOM 再查 NUL，否则 Windows 源文件会被整片误判）。
_UTF16_BOMS = (b"\xff\xfe", b"\xfe\xff")


def _looks_binary(target: Path) -> bool:
    """头部含 NUL 即判二进制；带 BOM 的 UTF-16 除外。"""
    try:
        with open(target, "rb") as handle:
            head = handle.read(_BINARY_PEEK)
    except OSError:  # 读不到头部就不拦，让真正的读取给出确切错误
        return False
    if head.startswith(_UTF16_BOMS):
        return False
    return b"\x00" in head


def _read_window(target: Path, offset: int, limit: int) -> tuple[list[str], int]:
    """流式取窗口，并把总行数数完 —— 只有窗口内容驻留内存。

    数完 `total_lines` 是为了它对前端的既有承诺（`ToolResultBlock.vue` 显示「N 行」）；
    代价是仍要扫一遍文件，换来的收益是内存从 O(文件大小) 降到 O(窗口)。

    **只在缓存未命中时走这里**（见 :func:`_read_page`）：同一文件连续翻页时，
    从第二页起改用 :func:`_read_window_only` + 缓存的行数，不再重复扫全文。

    **超大文件也不走这里**（见 `_read_window_uncounted`）：数行必须扫完全文，而 100MB
    日志每次翻页都扫 100MB 是纯浪费 —— 翻 10 页就是 1GB 的读放大。
    """
    page: list[str] = []
    total = 0
    with open(target, encoding="utf-8", errors="replace") as handle:
        for raw in handle:
            total += 1
            if total <= offset:
                continue
            if len(page) < limit:
                page.append(raw.rstrip("\n").rstrip("\r"))
    return page, total


def _read_window_only(target: Path, offset: int, limit: int) -> list[str]:
    """只取窗口，**不数总行**：读到窗口末尾就停。

    与 :func:`_read_window` 的差别只在于不需要扫完整个文件 —— 总行数由调用方从
    缓存提供。大文件深分页（`offset` 很大）时仍然要跳过前面所有行，但那只做计数、
    不做字符串处理，比"扫全文 + 数行 + 取窗口"轻得多。
    """
    page: list[str] = []
    with open(target, encoding="utf-8", errors="replace") as handle:
        skipped = 0
        for raw in handle:
            if skipped < offset:
                skipped += 1
                continue
            if len(page) >= limit:
                break
            page.append(raw.rstrip("\n").rstrip("\r"))
    return page


#: 总行数缓存：`str(path) -> (size, mtime_ns, total_lines)`。
#:
#: **为什么需要**：`total_lines` 每次都要数完整个文件，而前端/调用方每翻一页就要一次。
#: 同一文件连翻 10 页 = 10 次全文扫描（读放大量 = 页数 × 文件大小），这在长任务里是
#: 实打实的浪费 —— 模型常常先读 1-2000 行、再读 2001-4000 行、再读……
#:
#: **为什么用 (size, mtime) 做校验**：文件被改写后行数会变，只按路径缓存会给出过期的
#: 数字，进而让 `truncated` 判错（模型以为读完了、其实下面还有）。`st_mtime_ns` 足够
#: 细粒度地捕捉同秒内的改写。
_LINE_COUNT_CACHE: dict[str, tuple[int, int, int]] = {}
#: 缓存条数上限：长任务会在一个进程里读很多文件，不设限等于慢慢泄漏。
#: 满了就整体清空 —— 这里存的是可重算的派生数据，不做 LRU 精算。
_LINE_COUNT_CACHE_MAX = 256


#: 超过这个字节数的文件**不再统计总行数**。
#:
#: 分页读的本意是"文件不进内存、只驻留一个窗口"；而数总行必须扫完全文。对大文件，
#: 每翻一页就付一次全文扫描的代价（读放大 = 页数 × 文件大小），而 `total_lines`
#: 只用于前端显示与调用方猜"还有没有下文"——后者已由 `truncated` 精确给出。
#: 8MB 是分界：再往上，一次全文扫描的延迟已经超过翻页本身的收益。
_LINE_COUNT_MAX_BYTES = 8 * 1024 * 1024


def _read_window_uncounted(target: Path, offset: int, limit: int) -> tuple[list[str], bool]:
    """只取窗口、不数总行，用"窗口之后还有没有内容"判断截断。

    返回 ``(page, more)``，`more=True` 表示文件在窗口之后还有行 —— 它与
    `_read_window` 的 `truncated = offset + len(page) < total_lines` 同义，
    但只读到窗口末尾就停，不扫余下部分。
    """
    page: list[str] = []
    more = False
    with open(target, encoding="utf-8", errors="replace") as handle:
        skipped = 0
        for raw in handle:
            if skipped < offset:
                skipped += 1
                continue
            if len(page) < limit:
                page.append(raw.rstrip("\n").rstrip("\r"))
                continue
            more = True
            break
    return page, more


def _atomic_write_text(target: Path, content: str) -> None:
    """原子替换写：临时文件 + `os.replace`。

    临时文件必须落在**同一目录**：跨文件系统的 `os.replace` 不是原子操作（底层退化成
    拷贝 + 删除），那就失去了这么写的意义。名字带 pid 是为了并发写不同文件时互不干扰；
    同一文件的并发写本来就该由上层（`fs_guard`）串行化，这里不重复加锁。

    失败时清理临时文件 —— 否则一次磁盘满会在工作区里留下一堆 `.tmp` 垃圾，还会被
    `grep`/`ls` 扫到，干扰模型判断。
    """
    tmp = target.with_name(f".{target.name}.{os.getpid()}.tmp")
    try:
        with open(tmp, "w", encoding="utf-8", newline="") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, target)
    except OSError:
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass
        raise


def _append_text(target: Path, content: str) -> tuple[int, bool]:
    """追加写，返回 `(本次写入字节数, 是否补了换行)`。

    已有文件若不以换行结尾，直接追加会让新内容粘在最后一行上：模型按"多块拼一个
    文件"的方式写代码时，第二块的首行会跑到第一块末行的后面，拼出来的文件与它预期
    的差一个换行，随后的 `edit_file` 精确匹配就会失败。补一个换行让分块写的**结果**
    与"一次写完"一致。
    """
    padded = False
    needs_newline = False
    if target.exists() and target.stat().st_size > 0:
        with open(target, "rb") as probe:
            probe.seek(-1, os.SEEK_END)
            needs_newline = probe.read(1) not in (b"\n", b"\r")
    with open(target, "a", encoding="utf-8", newline="") as handle:
        if needs_newline:
            handle.write("\n")
            padded = True
        handle.write(content)
        handle.flush()
        os.fsync(handle.fileno())
    return len(content.encode("utf-8")) + (1 if padded else 0), padded


class LocalWorkspaceBackend:
    """`sandbox.safe_join` + 本地文件系统。"""

    async def read(self, path: str, *, offset: int = 0, limit: int = 200) -> ReadResult:
        target = safe_join(path)
        if not await asyncio.to_thread(target.exists):
            return ReadResult(path=str(target), error=f"file not found: {path}")

        if limit <= 0:
            # `limit <= 0` = 读**全文原文**（忽略 offset）：`edit_file` 需要逐字节精确的内容
            # 做唯一性校验与 diff —— 若这里做行归一化（`"\n".join(splitlines())`），
            # 原文末尾的换行会丢、CRLF 会被改写，替换语义随之漂移。
            text = await asyncio.to_thread(target.read_text, encoding="utf-8", errors="replace")
            return ReadResult(
                path=str(target),
                content=text,
                total_lines=len(text.splitlines()),
                offset=0,
                limit=0,
                truncated=False,
            )

        # 分页路径：先探头部判二进制，再**流式**取窗口 —— 文件不进内存，大文件才读得动。
        # 超大文件不再统计总行（见 `_read_page` 与 `_LINE_COUNT_MAX_BYTES`）。
        if await asyncio.to_thread(_looks_binary, target):
            return ReadResult(
                path=str(target),
                error=(
                    f"{path} looks like a binary file (NUL byte in its first "
                    f"{_BINARY_PEEK} bytes); read_file reads text"
                ),
            )
        return await self._read_page(target, offset, limit)

    async def _read_page(self, target: Path, offset: int, limit: int) -> ReadResult:
        """分页读的**实现体**：大文件走不数总行的快路径，小文件复用行数缓存。

        阈值判断放在这里而不是 `read`：`read` 还要处理二进制探测与错误包装，
        而"数不数总行"只跟文件大小有关，单独一层更容易读。

        `total_lines` 对大文件返回 `-1`（未知）：调用方（`read_file`）据此把
        提示写成"文件很大，未统计行数"，而不是给出一个**错误的具体数字**。

        **行数缓存**：`total_lines` 要扫完全文才数得出来，而调用方每翻一页
        都要问一次 —— 读 10 页就是 10 次全文扫描。缓存的 key 用 `(st_size, st_mtime_ns)`
        校验，文件一改就自然失效；命中时改走 :func:`_read_window_only`，只读到窗口末尾。
        """
        try:
            info = await asyncio.to_thread(lambda: target.stat())
            size = info.st_size
            stamp = (info.st_size, getattr(info, "st_mtime_ns", 0))
        except OSError:
            size = 0
            stamp = (0, 0)
        if size > _LINE_COUNT_MAX_BYTES:
            page, more = await asyncio.to_thread(_read_window_uncounted, target, offset, limit)
            return ReadResult(
                path=str(target),
                content="\n".join(page),
                total_lines=-1,
                offset=offset,
                limit=limit,
                truncated=more,
            )

        cache_key = str(target)
        cached = _LINE_COUNT_CACHE.get(cache_key)
        if cached is not None and cached[0] == stamp[0] and cached[1] == stamp[1]:
            # 命中：行数已知，只取窗口，不重扫全文。
            page = await asyncio.to_thread(_read_window_only, target, offset, limit)
            total_lines = cached[2]
        else:
            page, total_lines = await asyncio.to_thread(_read_window, target, offset, limit)
            if len(_LINE_COUNT_CACHE) >= _LINE_COUNT_CACHE_MAX:
                # 存的是可重算的派生数据，不做 LRU 精算：满了整体清空即可。
                _LINE_COUNT_CACHE.clear()
            _LINE_COUNT_CACHE[cache_key] = (stamp[0], stamp[1], total_lines)
        return ReadResult(
            path=str(target),
            content="\n".join(page),
            total_lines=total_lines,
            offset=offset,
            limit=limit,
            truncated=(offset + len(page)) < total_lines,
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
        """写文件（**原子替换**）。

        `pathlib.write_text` 是"先截断再写"：写到一半失败（磁盘满、进程被杀、容器被
        回收）会留下**半截文件**，而 undo 快照已经记成"写之前的状态"，回滚也救不回
        中间态。长任务里文件被反复重写，撞上的概率不低。

        改用"写临时文件 → `os.replace` 原子换名"：目标文件在任何时刻要么是旧的完整
        内容、要么是新的完整内容，不存在半截态。临时文件放在**同一目录**（跨文件系统
        的 `os.replace` 会降级成非原子的拷贝）；失败时清理临时文件，不留垃圾。
        """
        target = safe_join(path)
        await asyncio.to_thread(target.parent.mkdir, parents=True, exist_ok=True)
        data = content.encode("utf-8")
        try:
            await asyncio.to_thread(_atomic_write_text, target, content)
        except OSError as exc:
            return WriteResult(path=str(target), error=str(exc))
        return WriteResult(path=str(target), bytes_written=len(data))

    async def append(self, path: str, content: str) -> WriteResult:
        """把 `content` 追加到文件末尾（不存在则创建）。

        用 `open(..., "a")` 而不是"读全文 + 拼 + 重写"：大文件下后者要先把整个文件
        读进内存、再写一遍（O(2N)），而追加的语义本来就只需要动末尾。

        **不做原子替换**：追加的中间态是"已有内容 + 一部分新内容"，它在语义上可接受
        （不像整篇重写会先清空原有内容）。`a` 模式依赖 O_APPEND 把每次写入落在末尾。
        """
        target = safe_join(path)
        await asyncio.to_thread(target.parent.mkdir, parents=True, exist_ok=True)
        try:
            written, _padded = await asyncio.to_thread(_append_text, target, content)
        except OSError as exc:
            return WriteResult(path=str(target), error=str(exc))
        return WriteResult(path=str(target), bytes_written=written)

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
        # 走原子替换写（临时文件 + os.replace），不用 `write_text`：
        # 后者会**先截断再写**，进程在中间被杀 / 磁盘写满，留下的就是一个
        # 被清空或半截的文件 —— 原内容已经没了，这是不可恢复的损坏。
        updated = text.replace(old, new)
        try:
            await asyncio.to_thread(_atomic_write_text, target, updated)
        except OSError as exc:
            return EditResult(path=str(target), error=str(exc))
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
