"""E1：`full` 集的固件自检（**不需要模型**）。

固件是"任务的数据"，因此能被纯结构校验：分类覆盖、tier 标注、id 唯一、断言合法。
这里额外钉住一条运行口径 —— **`--suite full` 必须并入 smoke**：tier 的语义是"这条任务属于
哪一级门禁"，不是"它写在哪个文件里"。若 full 只读 `full.json`，nightly 门禁会比 PR 门禁还窄
（`smoke.json` 里大量任务标着 `["smoke","full"]`），那是危险的倒退。
"""

from __future__ import annotations

from collections import Counter
from pathlib import Path

import pytest

from evals.assertions import Observation, evaluate
from evals.cli import _load_scripted, _load_tasks
from evals.firmware import CATEGORIES, SUCCESS_KINDS, TIERS, Assertion, Efficiency, load_suite

FULL = "full"


def _full_tasks():
    return _load_tasks(FULL, "full")


# ── 固件结构 ────────────────────────────────────────────────────────────


def test_full_suite_loads():
    tasks = _load_tasks("full", None)

    assert tasks, "full.json 必须可加载"


def test_full_suite_size_matches_e1_acceptance():
    """E1 的验收口径：`full` 集 24–32 条。"""
    tasks = _full_tasks()

    assert 24 <= len(tasks) <= 32, f"full 集应有 24–32 条，实际 {len(tasks)}"


def test_full_suite_covers_every_category():
    """E1 的验收口径：覆盖全部 7 个分类。"""
    covered = Counter(t.category for t in _full_tasks())

    assert set(covered) == set(CATEGORIES), f"缺分类：{sorted(set(CATEGORIES) - set(covered))}"
    assert all(n >= 2 for n in covered.values()), f"每个分类至少要有 2 条：{dict(covered)}"


def test_full_only_tasks_are_marked_for_full_tier():
    """新增任务必须标 `full` —— 漏标会让它们**永远不被任何门禁跑到**。"""
    suite_path = Path(__file__).resolve().parents[1] / "evals" / "suites" / "full.json"

    for task in load_suite(suite_path):
        assert "full" in task.tiers, f"{task.id} 缺少 full tier"
        assert "smoke" not in task.tiers, f"{task.id} 是质量类任务，不该进 PR 门禁"


def test_every_task_has_assertions_and_known_tiers():
    for task in _full_tasks():
        assert task.assertions, f"{task.id} 没有断言"
        assert set(task.tiers) <= set(TIERS), f"{task.id} 的 tier 非法：{task.tiers}"
        for assertion in task.assertions:
            assert assertion.kind, f"{task.id} 断言缺 kind"


# ── 运行口径：full 并入 smoke ───────────────────────────────────────────


def test_full_tier_includes_smoke_suite_tasks_marked_full():
    """`--suite full` 必须把 `smoke.json` 里**标了 full** 的任务也带上。

    否则它们**只**在 PR 门禁里跑过 —— nightly 反而覆盖更少，那是倒退。
    （注意：`smoke.json` 里有若干任务显式只标 `smoke`，它们"只作 PR 门禁"是作者的意图，
    不该被这条断言要求进 full 集。）
    """
    smoke_path = Path(__file__).resolve().parents[1] / "evals" / "suites" / "smoke.json"
    smoke_full = {t.id for t in load_suite(smoke_path) if "full" in t.tiers}
    full_ids = {t.id for t in _full_tasks()}

    assert smoke_full, "smoke.json 里应当有标了 full 的任务（否则这条断言没有意义）"
    assert smoke_full <= full_ids, f"full 集漏掉了：{sorted(smoke_full - full_ids)}"


def test_duplicate_ids_across_suites_are_rejected():
    """跨文件查重：`load_suite` 只保证单文件内唯一，合并后必须自己把关。"""
    ids = [t.id for t in _load_tasks("smoke,full", None)]

    assert len(ids) == len(set(ids)), "合并两个固件后出现了重复 id"


def test_unknown_suite_raises():
    with pytest.raises(SystemExit):
        _load_tasks("no-such-suite", None)


def test_scripted_scripts_merge_without_crashing():
    """替身脚本缺失时就该是空表（full 集没有替身脚本，它只跑真实模型）。"""
    assert _load_scripted("no-such-suite") == {}
    assert isinstance(_load_scripted("smoke,full"), dict)


# ── 新增断言 kind ───────────────────────────────────────────────────────


def test_smoke_suite_exercises_every_assertion_kind():
    """**PR 门禁只跑 smoke** ⇒ `SUCCESS_KINDS` 里每一种都必须在 smoke 里出现。

    为什么需要这条：解析器与求值器支持一种 kind、`evals/README.md` 也写着"9 种断言 kind 全部进
    PR 门禁"，但只要 smoke 固件里**没有**用到它，它就**永远不会被执行** —— 这类"写了但没跑"的
    断言比没有更糟：它给人一种"已被验证"的错觉。`full` 只在 nightly 跑，不能替 PR 门禁兜底。
    """
    smoke = _load_tasks("smoke", "smoke")
    used = {a.kind for t in smoke for a in t.assertions}

    missing = sorted(SUCCESS_KINDS - used)
    assert not missing, (
        f"这些断言 kind 不在 PR 门禁的 smoke 集里，永远不会被执行：{missing}；"
        "要么在 evals/suites/smoke.json 补一条用到它的任务，要么把它从 SUCCESS_KINDS 去掉。"
    )


def test_file_not_contains_assertion():
    """`file_not_contains`：用于"改完了、旧值不该还在"这类断言（full 集需要）。"""
    obs = Observation(files={"a.py": "NEW = 1\n"})

    passed = evaluate(
        (Assertion(kind="file_not_contains", path="a.py", value="OLD"),), Efficiency(), obs
    )
    failed = evaluate(
        (Assertion(kind="file_not_contains", path="a.py", value="NEW"),), Efficiency(), obs
    )

    assert passed.passed is True
    assert failed.passed is False


def test_file_not_contains_treats_missing_file_as_absent():
    """文件不存在也算"不含" —— 问的是有没有这个内容，不是文件在不在。"""
    result = evaluate(
        (Assertion(kind="file_not_contains", path="ghost.txt", value="anything"),),
        Efficiency(),
        Observation(),
    )

    assert result.passed is True
