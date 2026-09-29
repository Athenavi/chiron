"""批 B 回归：文件工具必须经**后端**访问存储。

抽象的验收标准**不是**"文件还能读写"（那现有工具测试就够），而是
**"换一个后端实现，工具立刻跟着换"**。没有这条测试，某天有人把 `safe_join` 直接写回
工具里，抽象会静默失效而所有既有测试仍然全绿 —— 那正是这次抽象要避免的结局。
"""
from __future__ import annotations

from pathlib import Path

import pytest

from app.backends.context import get_backend, set_backend
from app.backends.local import LocalWorkspaceBackend
from app.backends.protocol import (
    BackendProtocol,
    EditResult,
    FileStat,
    GlobResult,
    GrepResult,
    LsResult,
    ReadBytesResult,
    ReadResult,
    WriteResult,
)
from app.tools.core import read_file, write_file


class _InMemoryBackend:
    """最小内存后端：只为证明"工具确实经后端访问"。"""

    def __init__(self) -> None:
        self.files: dict[str, str] = {}
        self.writes: list[str] = []

    async def read(self, path: str, *, offset: int = 0, limit: int = 200) -> ReadResult:
        if path not in self.files:
            return ReadResult(path=path, error=f"file not found: {path}")
        lines = self.files[path].splitlines()
        if limit <= 0:
            # `limit <= 0` = 读全文原文（与 LocalWorkspaceBackend 的约定一致）
            return ReadResult(
                path=path, content=self.files[path], total_lines=len(lines), limit=0
            )
        return ReadResult(
            path=path,
            content="\n".join(lines[offset : offset + limit]),
            total_lines=len(lines),
            offset=offset,
            limit=limit,
        )

    async def read_bytes(self, path: str) -> ReadBytesResult:
        if path not in self.files:
            return ReadBytesResult(path=path, error=f"file not found: {path}")
        return ReadBytesResult(path=path, data=self.files[path].encode("utf-8"))

    async def stat(self, path: str) -> FileStat | None:
        if path not in self.files:
            return None
        return FileStat(
            path=path, size=len(self.files[path].encode("utf-8")), is_file=True
        )

    async def write(self, path: str, content: str) -> WriteResult:
        self.files[path] = content
        self.writes.append(path)
        return WriteResult(path=path, bytes_written=len(content.encode("utf-8")))

    async def edit(self, path: str, old: str, new: str) -> EditResult:
        if path not in self.files:
            return EditResult(path=path, error=f"file not found: {path}")
        self.files[path] = self.files[path].replace(old, new)
        return EditResult(path=path, occurrences=1)

    async def ls(self, path: str = ".") -> LsResult:
        return LsResult(entries=sorted(self.files))

    async def glob(self, pattern: str, *, path: str = ".") -> GlobResult:
        return GlobResult(paths=sorted(self.files))

    async def grep(
        self, pattern: str, *, path: str = ".", max_results: int = 100
    ) -> GrepResult:
        return GrepResult()

    def supports_execution(self) -> bool:
        return False


@pytest.fixture(autouse=True)
def _restore_default_backend():
    """每个用例结束后恢复默认后端 —— contextvar 会跨用例残留。"""
    yield
    set_backend(None)


def test_memory_backend_satisfies_protocol():
    """结构化检查：内存替身确实符合 `BackendProtocol`（否则下面的结论无意义）。"""
    assert isinstance(_InMemoryBackend(), BackendProtocol)
    assert isinstance(LocalWorkspaceBackend(), BackendProtocol)


def test_default_backend_is_local_workspace():
    """不注入时就是今天的本地工作区行为 —— 这是"零行为漂移"的前提。"""
    assert isinstance(get_backend(), LocalWorkspaceBackend)


@pytest.mark.asyncio
async def test_read_file_uses_injected_backend():
    backend = _InMemoryBackend()
    backend.files["a.txt"] = "line1\nline2"
    set_backend(backend)

    out = await read_file("a.txt")
    assert out["content"] == "line1\nline2"
    assert out["total_lines"] == 2
    # 缺失文件走后端的错误消息（与迁移前一致）
    assert "file not found" in (await read_file("missing.txt"))["error"]


@pytest.mark.asyncio
async def test_write_file_uses_injected_backend():
    backend = _InMemoryBackend()
    set_backend(backend)

    out = await write_file("dir/b.txt", "hello")
    assert backend.writes == ["dir/b.txt"], "写入未经过注入的后端"
    assert backend.files["dir/b.txt"] == "hello"
    assert out["bytes"] == 5


@pytest.mark.asyncio
async def test_local_backend_write_creates_parent_dirs(tmp_path: Path, monkeypatch):
    """建父目录由后端负责（迁移前由工具做）—— 行为必须保持。"""
    monkeypatch.setenv("SANDBOX_ROOT", str(tmp_path))
    from app.tools.sandbox import workspace_dir

    backend = LocalWorkspaceBackend()
    await backend.write("nested/deep/file.txt", "x")

    # 期望路径由 `workspace_dir()` 给出（它含 per-user 隔离段），而不是手拼 —— 否则
    # 测试会把"隔离目录的形状"写死，日后调整目录结构就误报。
    expected = workspace_dir() / "nested" / "deep" / "file.txt"
    assert expected.read_text(encoding="utf-8") == "x"


@pytest.mark.asyncio
async def test_local_backend_reports_truncation_itself(tmp_path: Path, monkeypatch):
    """协议约束 2：`truncated` 由**产生截断的那一层**给出，不由调用方猜。"""
    monkeypatch.setenv("SANDBOX_ROOT", str(tmp_path))
    backend = LocalWorkspaceBackend()
    await backend.write("many.txt", "\n".join(f"line{i}" for i in range(50)))

    page = await backend.read("many.txt", offset=0, limit=10)
    assert page.truncated is True

    whole = await backend.read("many.txt", offset=0, limit=1000)
    assert whole.truncated is False


# ── 片 2：其余文件工具同样经后端 ──────────────────────────────────────────


@pytest.mark.asyncio
async def test_edit_file_uses_injected_backend():
    """`edit_file` 需要**逐字节精确**的原文 —— 走的是 `read(limit=0)` 的全文语义。"""
    from app.tools.edit_file import edit_file

    backend = _InMemoryBackend()
    backend.files["a.txt"] = "hello world\n"
    set_backend(backend)

    out = await edit_file("a.txt", old_string="world", new_string="there")
    assert out.get("success") is True, out
    assert backend.files["a.txt"] == "hello there\n", "写入未经过注入的后端"
    # diff 是**行级**的：改一个词也会整行呈现为 -旧行/+新行
    assert "+hello there" in out["diff"]


@pytest.mark.asyncio
async def test_edit_file_preserves_trailing_newline(tmp_path: Path, monkeypatch):
    """`limit=0` 必须给**原文**：若做行归一化，末尾换行会丢、替换语义随之漂移。"""
    from app.tools.edit_file import edit_file
    from app.tools.sandbox import workspace_dir

    monkeypatch.setenv("SANDBOX_ROOT", str(tmp_path))
    set_backend(None)  # 用真实本地后端，验证逐字节保真
    target = workspace_dir() / "keep.txt"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("a\nb\n", encoding="utf-8")

    await edit_file("keep.txt", old_string="b", new_string="c")
    assert target.read_text(encoding="utf-8") == "a\nc\n"


@pytest.mark.asyncio
async def test_glob_and_search_use_injected_backend():
    from app.tools.core import search_files
    from app.tools.glob_tools import glob_files

    backend = _InMemoryBackend()
    backend.files["src/a.py"] = "x"
    set_backend(backend)

    globbed = await glob_files("**/*.py")
    assert globbed["count"] == 1, "glob_files 未经过注入的后端"

    searched = await search_files("*.py")
    assert searched["count"] == 1, "search_files 未经过注入的后端"


@pytest.mark.asyncio
async def test_grep_files_uses_injected_backend():
    from app.tools.core import grep_files

    backend = _InMemoryBackend()
    backend.files["src/a.py"] = "import os\nTARGET = 1\n"
    set_backend(backend)

    out = await grep_files("TARGET")
    assert out["count"] == 1
    assert out["matches"][0]["line"] == 2


@pytest.mark.asyncio
async def test_read_image_uses_injected_backend():
    from app.tools.core import read_image

    backend = _InMemoryBackend()
    backend.files["pixel.png"] = "fake-png-bytes"
    set_backend(backend)

    out = await read_image("pixel.png")
    assert out["media_type"] == "image/png"
    assert out["bytes"] == len(b"fake-png-bytes")
    assert out["data_url"].startswith("data:image/png;base64,")


# ── 片 3：Composite 前缀路由 ──────────────────────────────────────────────


@pytest.mark.asyncio
async def test_composite_routes_by_prefix():
    from app.backends.composite import CompositeBackend

    default = _InMemoryBackend()
    artifacts = _InMemoryBackend()
    default.files["a.txt"] = "default"
    artifacts.files["x.txt"] = "artifact"
    backend = CompositeBackend(default, {"/artifacts/": artifacts})

    assert (await backend.read("/artifacts/x.txt")).content == "artifact"
    assert (await backend.read("a.txt")).content == "default"


@pytest.mark.asyncio
async def test_composite_write_routes_to_mounted_backend():
    from app.backends.composite import CompositeBackend

    default = _InMemoryBackend()
    artifacts = _InMemoryBackend()
    backend = CompositeBackend(default, {"/artifacts/": artifacts})

    await backend.write("/artifacts/new.txt", "hi")
    assert artifacts.files["new.txt"] == "hi", "未路由到挂载的后端"
    assert default.writes == [], "不该同时写到默认后端"


def test_composite_normalizes_mounts():
    from app.backends.composite import CompositeBackend

    backend = CompositeBackend(_InMemoryBackend(), {"artifacts": _InMemoryBackend()})
    # `artifacts` / `/artifacts` / `/artifacts/` 等价
    assert backend.mounts == ["/artifacts/"]


@pytest.mark.asyncio
async def test_composite_longest_prefix_routes_correctly():
    """最长前缀优先 —— 验证**路由行为**而不是内部排序（后者只是手段）。"""
    from app.backends.composite import CompositeBackend

    outer = _InMemoryBackend()
    inner = _InMemoryBackend()
    outer.files["x.txt"] = "outer"
    inner.files["x.txt"] = "inner"
    backend = CompositeBackend(
        _InMemoryBackend(), {"/a/": outer, "/a/b/": inner}
    )

    assert (await backend.read("/a/b/x.txt")).content == "inner"
    assert (await backend.read("/a/x.txt")).content == "outer"


@pytest.mark.asyncio
async def test_composite_execute_requires_executing_default():
    """执行能力只看默认后端；不支持时必须**明确报错**，不静默挑一个后端。

    对位 deepagents 的同名约束：`execute` 与 `read`/`write` 必须落在同一文件系统，
    否则"命令写出的文件"与"工具读到的文件"会分叉。
    """
    from app.backends.composite import CompositeBackend

    backend = CompositeBackend(_InMemoryBackend(), {})
    result = await backend.execute("echo hi")
    assert result.error is not None
    assert "does not support execution" in result.error


@pytest.mark.asyncio
async def test_composite_with_local_default_executes(tmp_path: Path, monkeypatch):
    from app.backends.composite import CompositeBackend

    monkeypatch.setenv("SANDBOX_ROOT", str(tmp_path))
    backend = CompositeBackend(LocalWorkspaceBackend(), {"/artifacts/": _InMemoryBackend()})
    assert backend.supports_execution() is True

    # 用 python 而不是 echo：它在白名单内且跨平台行为一致
    result = await backend.execute('python -c "print(\'composite-exec\')"')
    assert "composite-exec" in result.output


# ── 片 4：经网关 FileStore 的后端 ─────────────────────────────────────────


class _FakeGateway:
    """替身网关：在内存里实现 `/v1/internal/storage/*` 的语义。

    这样能验证 `FileStoreBackend` 的**协议行为**（路径语义、404、分页、glob 转换），
    而不必起真实网关；Go 侧 handler 的真实行为由 `internal/api` 的 Go 测试覆盖。
    """

    def __init__(self) -> None:
        self.files: dict[str, str] = {}

    async def __call__(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, str] | None = None,
        body: dict[str, object] | None = None,
    ) -> tuple[int, dict]:
        params = params or {}
        if path.endswith("/write"):
            assert body is not None
            self.files[str(body["path"])] = str(body["content"])
            return 200, {"path": body["path"], "size": len(str(body["content"]))}
        if path.endswith("/read"):
            key = params.get("path", "")
            if key not in self.files:
                return 404, {"error": "not found"}
            return 200, {
                "path": key,
                "content": self.files[key],
                "encoding": "utf-8",
                "size": len(self.files[key]),
            }
        if path.endswith("/list"):
            prefix = params.get("prefix", "")
            return 200, {
                "files": [
                    {"path": k, "size": len(v), "is_dir": False, "modified": ""}
                    for k, v in sorted(self.files.items())
                    if not prefix or k.startswith(prefix)
                ]
            }
        return 500, {"error": "unsupported"}


def _filestore_with_fake_gateway() -> tuple[object, _FakeGateway]:
    from app.backends.filestore import FileStoreBackend

    backend = FileStoreBackend("http://gateway.test", "internal-token")
    gateway = _FakeGateway()
    backend._call = gateway  # type: ignore[method-assign] — 测试替身，不起真实网关
    return backend, gateway


@pytest.mark.asyncio
async def test_filestore_backend_write_read_round_trip():
    backend, _ = _filestore_with_fake_gateway()

    written = await backend.write("docs/a.md", "hello")
    assert written.error is None and written.bytes_written == 5

    read = await backend.read("docs/a.md", limit=0)
    assert read.error is None and read.content == "hello"


@pytest.mark.asyncio
async def test_filestore_backend_missing_file_is_an_error_not_empty():
    """方案 02 §3.4 的完成标准：必须区分"没有这个文件"与"文件是空的"。"""
    backend, _ = _filestore_with_fake_gateway()

    read = await backend.read("nope.md", limit=0)
    assert read.error is not None and "file not found" in read.error
    assert read.content == ""


@pytest.mark.asyncio
async def test_filestore_backend_unconfigured_reports_error():
    """未配置网关地址时明确失败，而不是静默读到空。"""
    from app.backends.filestore import FileStoreBackend

    backend = FileStoreBackend("", "")
    read = await backend.read("a.md", limit=0)
    assert read.error is not None
    assert "not configured" in read.error


@pytest.mark.asyncio
async def test_filestore_backend_glob_matches_nested_paths():
    """`**/*.py` 必须匹配子目录 —— `fnmatch` 会把 `*` 当成跨 `/`，那就会错配。"""
    backend, _ = _filestore_with_fake_gateway()
    await backend.write("src/a.py", "x")
    await backend.write("src/deep/b.py", "y")
    await backend.write("src/c.txt", "z")

    globbed = await backend.glob("**/*.py")
    assert globbed.paths == ["src/a.py", "src/deep/b.py"]

    # 单层 `*.py` 不该匹配子目录
    shallow = await backend.glob("*.py")
    assert shallow.paths == []


@pytest.mark.asyncio
async def test_filestore_backend_stat_uses_list_not_full_read():
    backend, gateway = _filestore_with_fake_gateway()
    await backend.write("a.md", "12345")

    info = await backend.stat("a.md")
    assert info is not None and info.size == 5 and info.is_file

    assert await backend.stat("missing.md") is None


def test_filestore_backend_does_not_support_execution():
    """FileStore 只存文件 —— 调用方据此把执行类工具从工具面剔除。"""
    backend, _ = _filestore_with_fake_gateway()
    assert backend.supports_execution() is False
    assert isinstance(backend, BackendProtocol)


def test_set_default_backend_is_process_level():
    """启动期配置必须落**进程级**。

    `set_backend` 写的是 contextvar，而 lifespan 里设置的 contextvar 不会被后续请求任务
    继承（请求任务由 ASGI server 派生，不是 lifespan 的子任务）—— 那会变成"配了 filestore
    但请求里仍走 local"，而日志看起来一切正常。
    """
    from app.backends.context import set_default_backend

    backend = _InMemoryBackend()
    try:
        set_default_backend(backend)
        assert get_backend() is backend
    finally:
        set_default_backend(None)  # 复原：下次 get_backend() 会惰性重建本地后端


# ── 片 5 / 片 6：输出治理与能力协商 ──────────────────────────────────────


def test_execute_output_truncation_marks_omitted_bytes():
    """片 5：截断必须**显式标注省略量** —— 静默截断会让模型误判"输出就这么短"。"""
    from app.tools.sandbox import (
        EXECUTE_HEAD_BYTES,
        MAX_EXECUTE_OUTPUT_BYTES,
        truncate_execute_output,
    )

    small, truncated = truncate_execute_output("hello")
    assert small == "hello" and truncated is False

    huge = "x" * (MAX_EXECUTE_OUTPUT_BYTES + 10_000)
    cut, truncated = truncate_execute_output(huge)
    assert truncated is True
    assert "bytes truncated" in cut
    assert len(cut.encode("utf-8")) < len(huge.encode("utf-8"))
    # head 保留（前 EXECUTE_HEAD_BYTES 字节的内容可见）
    assert cut.startswith("x" * EXECUTE_HEAD_BYTES)


@pytest.mark.asyncio
async def test_execute_result_reports_truncation(tmp_path: Path, monkeypatch):
    """片 5：`ExecuteResult.truncated` 由产生截断的那一层给出（协议约束 2）。"""
    from app.tools.sandbox import MAX_EXECUTE_OUTPUT_BYTES

    monkeypatch.setenv("SANDBOX_ROOT", str(tmp_path))
    backend = LocalWorkspaceBackend()

    # 让命令输出超过上限（用 python 而不是 shell 内建：它在白名单内且跨平台一致）
    code = f"print('y' * {MAX_EXECUTE_OUTPUT_BYTES + 5_000})"
    result = await backend.execute(f'python -c "{code}"')

    assert result.exit_code == 0, "截断不该影响退出码"
    assert result.truncated is True
    assert "bytes truncated" in result.output


@pytest.mark.asyncio
async def test_runtime_drops_execution_tools_when_backend_cannot_execute():
    """片 6：后端不支持执行时，执行类工具不该出现在工具面里。

    否则模型会反复调用一个必然失败的工具（每次一次往返 + 一条失败结果进上下文）。
    """
    from unittest.mock import MagicMock

    from app.agent.runtime import AgentRuntime, AgentTask
    from app.gateway.provider import ChatResponse

    captured: list[dict] = []
    gateway = MagicMock()

    async def fake_stream(**kwargs):
        captured.append(kwargs)
        yield ChatResponse(content="ok", finish_reason="stop")

    gateway.chat_stream = fake_stream
    set_backend(_InMemoryBackend())  # supports_execution() == False

    runtime = AgentRuntime(gateway=gateway)
    task = AgentTask(
        id="t",
        tenant_id="t",
        user_id="u",
        session_id="",
        content="hi",
        system_prompt="sp",
        llm_config={"mode": "normal"},
        max_turns=1,
    )
    _ = [event async for event in runtime.run(task)]

    names = {t["function"]["name"] for t in captured[0]["tools"]}
    assert "read_file" in names, "读类工具不该被一起剔除"
    assert not (names & {"shell_exec", "run_code", "persistent_shell"}), (
        f"后端不支持执行，但工具面里仍有执行类工具：{names}"
    )
