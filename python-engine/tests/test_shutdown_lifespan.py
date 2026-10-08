"""`_shutdown_lifespan`：关闭清理的**句柄显式传递**契约。

为什么值得测：关闭段此前藏在 `lifespan` 里、靠 `'x' in locals()` 探测"启动时到底建没建"，
**没有任何用例**。把它抽出来时踩到的坑是——一旦启动段被抽成函数，这些名字就不在 `lifespan`
的 locals 里了，清理会**静默跳过**（任务不取消、实例不注销，还不报错）。
所以这里钉两件事：① 传进来的句柄**一定**被取消/停止；② 传 None 时安全跳过（这正是当年
`locals()` 探测想表达的意思，现在由显式参数表达）。
"""

import ast
import asyncio
import pathlib

from app import main as main_mod
from app.main import _shutdown_lifespan

MAIN_PY = pathlib.Path(__file__).resolve().parents[1] / "app" / "main.py"


async def _running_task() -> asyncio.Task:
    started = asyncio.Event()

    async def forever() -> None:
        started.set()
        await asyncio.sleep(3600)

    task = asyncio.create_task(forever())
    await started.wait()
    return task


class _FakeRegistry:
    def __init__(self) -> None:
        self.stopped = 0

    async def stop(self) -> None:
        self.stopped += 1


class _FakePool:
    def __init__(self) -> None:
        self.stopped = 0

    async def stop(self) -> None:
        self.stopped += 1


async def test_shutdown_cancels_every_given_handle(monkeypatch) -> None:
    reconciler = await _running_task()
    metrics = await _running_task()
    retention = await _running_task()
    registry = _FakeRegistry()
    pool = _FakePool()
    monkeypatch.setattr(main_mod, "_plugin_pool", pool)

    await _shutdown_lifespan(
        reconciler_task=reconciler,
        metrics_task=metrics,
        retention_task=retention,
        engine_registry=registry,
    )

    for name, task in (("reconciler", reconciler), ("metrics", metrics), ("retention", retention)):
        assert task.cancelled() or task.done(), f"{name} 任务没有被取消（清理被跳过）"
    assert registry.stopped == 1, "实例注册心跳没有主动注销"
    assert pool.stopped == 1, "MCP 插件池没有关闭"
    assert main_mod._plugin_pool is None, "关闭后插件池应被置空"


async def test_shutdown_without_handles_is_safe() -> None:
    """启动阶段失败（没有建任何句柄）时，关闭段必须安全通过 —— 这是 None 分支的意义。"""
    await _shutdown_lifespan()


def test_lifespan_does_not_probe_locals_anymore() -> None:
    """机械护栏：`lifespan` 里**不许**再出现 `locals()` 探测。

    那种写法把"清理是否执行"绑在**词法作用域**上：任何把启动段抽成函数的重构都会让它
    静默失效（见 docs/split-assessment.md §2.3）。句柄必须显式传递。
    """
    tree = ast.parse(MAIN_PY.read_text(encoding="utf-8"))
    lifespan = next(
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.AsyncFunctionDef) and node.name == "lifespan"
    )
    offenders = [
        node.lineno
        for node in ast.walk(lifespan)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "locals"
    ]
    assert not offenders, (
        f"lifespan 第 {offenders} 行又用 locals() 探测句柄了 —— 改成显式参数传递"
        "（否则关闭清理会在重构后静默跳过）"
    )
    assert any(
        isinstance(node, ast.AsyncFunctionDef) and node.name == "_shutdown_lifespan"
        for node in tree.body
    ), "关闭清理应独立成 _shutdown_lifespan（句柄显式传入）"
