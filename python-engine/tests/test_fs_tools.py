"""`ls` 工具（P1-② 切口 4）：非递归列目录。

为什么单独这一组：`ls` 的验收**不是**"能列目录"（那太弱），而是三条边界 ——
① 越界路径被拒（`safe_join` 语义）；② 不存在的路径返回 error 而不是抛；
③ 访问**经后端**（换后端即跟着换，与 `tests/test_backends.py` 同一口径 ——
否则有人改回直连文件系统时，抽象会静默失效而测试仍全绿）。
"""

from __future__ import annotations

import pytest

import app.tools  # noqa: F401 — 工具注册的唯一入口（见 app/tools/__init__.py）
from app.backends.context import set_backend
from app.tools.fs_tools import list_dir
from app.tools.registry import registry


def test_ls_is_registered():
    assert "ls" in registry.list_names()


@pytest.mark.asyncio
async def test_ls_lists_workspace_entries():
    from app.tools.sandbox import workspace_dir

    probe = workspace_dir() / "ls_probe.tmp"
    probe.write_text("x", encoding="utf-8")
    try:
        out = await list_dir(".")

        assert out["count"] >= 1
        assert any(e.endswith("ls_probe.tmp") for e in out["entries"])
    finally:
        probe.unlink(missing_ok=True)


@pytest.mark.asyncio
async def test_ls_missing_path_returns_error():
    out = await list_dir("no-such-dir-xyz")

    assert "error" in out, "不存在的路径应返回 error，而不是异常"
    assert out["count"] == 0


@pytest.mark.asyncio
async def test_ls_rejects_sandbox_escape():
    out = await list_dir("../../")

    assert out.get("error") == "path escapes sandbox", "越界路径必须被拒（沙箱隔离）"


@pytest.mark.asyncio
async def test_ls_goes_through_the_backend():
    from app.backends.protocol import LsResult

    seen: list[str] = []

    class _RecordingBackend:
        async def ls(self, path: str = ".") -> LsResult:
            seen.append(path)
            return LsResult(entries=["/virtual/a.txt"])

    set_backend(_RecordingBackend())  # type: ignore[arg-type] — 只实现 ls 的最小替身
    try:
        out = await list_dir("some/dir")
    finally:
        set_backend(None)  # contextvar 不随用例结束消失，必须显式恢复

    assert seen == ["some/dir"], "工具必须把路径交给后端，而不是自己读文件系统"
    assert out["entries"] == ["/virtual/a.txt"]


# ── delete_file（破坏性：动作分级为 delete） ────────────────────────────────


def test_delete_file_is_registered():
    assert "delete_file" in registry.list_names()


def test_delete_file_is_classified_as_delete_on_both_sides():
    """破坏性动作必须两侧都登记为 delete —— 否则只会多一次 write 确认就直接删了。

    Go 侧的同构由 `scripts/check_tool_policy_parity.py` 断言（本用例只守 Python 侧）。
    """
    from app.agent import tool_policy

    assert tool_policy._TOOL_LEVELS["delete_file"] == tool_policy.DELETE


@pytest.mark.asyncio
async def test_delete_file_removes_the_file():
    from app.tools.fs_tools import delete_file
    from app.tools.sandbox import workspace_dir

    probe = workspace_dir() / "delete_probe.tmp"
    probe.write_text("x", encoding="utf-8")

    out = await delete_file("delete_probe.tmp")

    assert out["deleted"] is True
    assert not probe.exists()


@pytest.mark.asyncio
async def test_delete_file_is_idempotent_on_missing_path():
    """删除不存在的路径**不是错误** —— 目标状态（不存在）已经达成。"""
    from app.tools.fs_tools import delete_file

    out = await delete_file("no-such-file-xyz.tmp")

    assert out["deleted"] is False
    assert "error" not in out


@pytest.mark.asyncio
async def test_delete_file_refuses_a_directory():
    """只删文件：递归删目录是另一个更危险的动作，不做隐式扩展。"""
    from app.tools.fs_tools import delete_file

    out = await delete_file(".")

    assert "error" in out
    assert "directory" in out["error"]


@pytest.mark.asyncio
async def test_delete_file_rejects_sandbox_escape():
    from app.tools.fs_tools import delete_file

    out = await delete_file("../../etc/passwd")

    assert out.get("error") == "path escapes sandbox"
