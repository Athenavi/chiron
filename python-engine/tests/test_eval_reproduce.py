"""对标编排脚本（`evals.reproduce`）的契约。

钉住三件最容易腐化的事：

1. **缺前置必须明确失败** —— 否则会跑出一份"看起来正常"的报告被误读（Chiron 侧没起来 ⇒
   报告里全是失败 ⇒ 被读成"对手更强"）；
2. **修复命令要跟着前置项一起给出** —— 只说"缺 key"而不说怎么配，等于没说；
3. **固件指纹可算** —— "可复现"的判据就是"同哈希 + 同模型"。
"""

from __future__ import annotations

import json

from evals.reproduce import _compare, check_preconditions, fixture_fingerprint


def _by_name(checks: list[object]) -> dict[str, object]:
    return {c.name: c for c in checks}  # type: ignore[attr-defined]


def test_missing_deepagents_deps_is_reported_with_the_fix_command():
    checks = check_preconditions(
        base_url="", api_key="k", need_stateful_stack=False, has_deepagents_deps=False
    )
    bad = _by_name([c for c in checks if not c.ok])  # type: ignore[attr-defined]

    assert "deepagents 依赖" in bad
    assert "pip install -e" in bad["deepagents 依赖"].detail  # type: ignore[attr-defined]


def test_missing_api_key_is_reported():
    checks = check_preconditions(
        base_url="", api_key="", need_stateful_stack=False, has_deepagents_deps=True
    )

    assert any(c.name == "模型密钥" and not c.ok for c in checks)


def test_stack_is_only_checked_when_it_is_needed():
    """`--check`（零副作用）不该因为"栈没起"就失败 —— 那不是它的体检项。"""
    offline = check_preconditions(
        base_url="", api_key="k", need_stateful_stack=False, has_deepagents_deps=True
    )
    assert not any(c.name == "Chiron 栈可达" for c in offline)

    online = check_preconditions(
        base_url="http://127.0.0.1:8080",
        api_key="k",
        need_stateful_stack=True,
        has_deepagents_deps=True,
        stack_healthy=False,
    )
    assert any(c.name == "Chiron 栈可达" and not c.ok for c in online)


def test_all_green_when_everything_is_present():
    checks = check_preconditions(
        base_url="http://127.0.0.1:8080",
        api_key="k",
        need_stateful_stack=True,
        has_deepagents_deps=True,
        stack_healthy=True,
    )

    assert all(c.ok for c in checks), [c for c in checks if not c.ok]


def test_fixture_fingerprint_is_stable_and_nonempty():
    fingerprint = fixture_fingerprint()

    assert fingerprint["full"], "full 固件必须存在（对标的口径就是它）"
    assert fingerprint["smoke"], "full tier 会并入 smoke 里标 full 的任务，指纹要一起算"
    assert fingerprint == fixture_fingerprint(), "指纹必须确定性 —— 否则'同哈希'无从谈起"


def test_compare_merges_two_reports_into_a_category_diff(tmp_path):
    paths = []
    for agent, passes in (("chiron", 1), ("deepagents", 0)):
        path = tmp_path / f"{agent}.json"
        path.write_text(
            json.dumps(
                {
                    "suite": "full",
                    "agent": agent,
                    "tasks": [
                        {
                            "task_id": "t1",
                            "category": "cat",
                            "attempts": 1,
                            "passes": passes,
                        }
                    ],
                }
            ),
            encoding="utf-8",
        )
        paths.append(path)

    out = tmp_path / "compare.json"
    table = _compare(paths, out)

    assert out.exists(), "对照表要落盘（可对外引用）"
    assert table["by_category_diff"]["cat"]["_spread"] == 1.0  # type: ignore[index]
