"""装配守卫：L3 巩固链路的四个环节必须在 `app/main.py` 里接齐。

这一条缺陷链是四个独立断点叠起来的，每一个都只表现为"记忆没生效"而不是报错：

1. `MemoryService` 没接 `producer` ⇒ 全库唯一的 `memory_consolidate` 生产者从不运行；
2. `QueueWorker` 没接 `memory_service` ⇒ handler 一进门就抛，任务烧进 DLQ；
3. `context/manager.py` 按一个不存在的签名调 `save_summary` ⇒ TypeError 被宽 except 咽掉；
4. worker 里就地 `Consolidator(store=…)` ⇒ 没有 embedder/summariser，与主装配口径分裂。

3、4 由各自的模块单测钉住（形状与调用点）。这里钉 1、2 这一类**只看装配才看得出来**的问题：
参数少写一个不会有任何症状，只有链路末端"一条摘要也没写出来"，而那要查很久。
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from app import __file__ as app_init

ENGINE = Path(app_init).parent


def _calls(path: Path) -> list[ast.Call]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    return [n for n in ast.walk(tree) if isinstance(n, ast.Call)]


def _kwarg_names(call: ast.Call) -> set[str]:
    return {k.arg for k in call.keywords if k.arg}


def _build_calls(path: Path, func_name: str) -> list[ast.Call]:
    return [
        c
        for c in _calls(path)
        if isinstance(c.func, ast.Name) and c.func.id == func_name
    ]


@pytest.mark.parametrize(
    ("func_name", "required"),
    [
        # `producer` 少一个字母整条 L3 就断：入队点是三处，全都 `if not self._producer: return`
        ("MemoryService", {"consolidator", "summary_store", "session_meta_store", "producer"}),
        # 巩固与 rollup 的 handler 都靠它；没有它它们是"必然失败的后台任务"
        ("QueueWorker", {"memory_service"}),
    ],
)
def test_main_passes_every_dependency_the_chain_needs(func_name: str, required: set[str]):
    calls = _build_calls(ENGINE / "main.py", func_name)
    assert calls, f"app/main.py 里找不到 {func_name}(...) —— 装配点挪走了，这条守卫要跟着改"

    for call in calls:
        missing = required - _kwarg_names(call)
        assert not missing, f"app/main.py:{call.lineno} {func_name}(...) 缺参数 {sorted(missing)}"


def test_there_is_exactly_one_consolidator_assembly():
    """全进程只建一个 `Consolidator`，且它带齐 embedder 与 summariser。

    第二处装配 = 第二套口径：抽取式正文 + 恒 NULL 向量，而且它是后台任务默认走到的那一套。
    """
    calls = _build_calls(ENGINE / "main.py", "Consolidator")
    assert len(calls) == 1

    given = _kwarg_names(calls[0])
    assert {"store", "embedder", "summariser"} <= given, (
        f"app/main.py:{calls[0].lineno} Consolidator 缺 {sorted({'store', 'embedder', 'summariser'} - given)}"
    )


def test_the_worker_module_cannot_build_its_own_consolidator():
    """worker 侧只许经 `MemoryService.save_summary` 复用主装配 —— 源码里不该有这个名字。"""
    assert not _build_calls(ENGINE / "queue" / "worker.py", "Consolidator")


def test_the_context_manager_does_not_write_l3_by_hand():
    """降级内容只能经巩固链路进 L3：绕过它就没有向量、没有判重、正文口径也另起一套。"""
    manager = ENGINE / "context" / "manager.py"
    text = manager.read_text(encoding="utf-8")

    assert "SummaryStore(" not in text
    assert "save_summary" in text, "降级链路整个不写 L3 也是一种断"
