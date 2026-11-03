"""P3：结构化计划契约（app/agent/plan.py）单测。

钉住三条不变量：host 赋 id/revision；verified ⊆ candidate；至多一个 in_progress。
"""

from __future__ import annotations

from app.agent.plan import Plan, PlanStep, normalize, ordered, render, to_todos, validate


def test_normalize_assigns_host_ids() -> None:
    plan, errors = normalize([{"content": "do a"}, {"content": "do b"}])
    assert errors == []
    assert [s.id for s in plan.steps] == ["t1", "t2"]


def test_normalize_ignores_model_supplied_id_as_identity() -> None:
    # 模型给的 id 不被采信为身份（host 一律重编 s1/s2…）。
    plan, errors = normalize([{"id": "evil", "content": "x"}])
    assert errors == []
    assert plan.steps[0].id == "t1"


def test_normalize_rejects_verified_not_in_candidates() -> None:
    plan, errors = normalize(
        [{"content": "x", "verified": ["a.py"]}]  # 没列 candidates 却声称验证过
    )
    assert any("verified files not among its candidates" in e for e in errors)
    assert plan.steps[0].verified_files == ("a.py",)


def test_normalize_separates_candidate_and_verified() -> None:
    plan, errors = normalize(
        [{"content": "x", "files": ["a.py", "b.py"], "verified": ["a.py"]}]
    )
    assert errors == []
    assert plan.steps[0].candidate_files == ("a.py", "b.py")
    assert plan.steps[0].verified_files == ("a.py",)


def test_normalize_rejects_two_unowned_in_progress() -> None:
    # 无主的 in_progress 无法归属，也就无法并行 —— 仍然只许一个。
    _, errors = normalize(
        [
            {"content": "a", "status": "in_progress"},
            {"content": "b", "status": "in_progress"},
        ]
    )
    assert any("at most one unowned step may be in_progress" in e for e in errors)


def test_normalize_allows_parallel_in_progress_across_owners() -> None:
    """多 agent 共享看板：不同认领者各领一项同时进行，是**合法**状态。

    旧规则"整份计划至多一个 in_progress"来自单 agent 假设。协作时几个子 agent 并行
    做工，多项同时 in_progress 才符合事实；若按旧规则整份拒绝，板子永远写不进去，
    协作直接不可用。
    """
    plan, errors = normalize(
        [
            {"content": "a", "status": "in_progress", "owner": "run-1"},
            {"content": "b", "status": "in_progress", "owner": "run-2"},
        ]
    )
    assert errors == []
    assert [s.owner for s in plan.steps] == ["run-1", "run-2"]


def test_normalize_rejects_two_in_progress_for_same_owner() -> None:
    """但同一个 agent 同时宣称在做两件事，说明它计划自相矛盾或忘了收口。"""
    _, errors = normalize(
        [
            {"content": "a", "status": "in_progress", "owner": "main"},
            {"content": "b", "status": "in_progress", "owner": "main"},
        ]
    )
    assert any("at most one per owner" in e for e in errors)


def test_normalize_rejects_dangling_dependency() -> None:
    """悬空依赖让"何时能开工"永远算不出来，且模型写 t9 往往只是笔误。"""
    _, errors = normalize([{"content": "a", "depends_on": ["t9"]}])
    assert any("depends on unknown step" in e for e in errors)


def test_normalize_rejects_self_dependency() -> None:
    """自依赖是死锁：等自己完成才能开工，永远不会开工。"""
    _, errors = normalize([{"content": "a", "depends_on": ["t1"]}])
    assert any("depends on itself" in e for e in errors)


def test_normalize_rejects_dependency_cycle() -> None:
    """环会让两个 agent 互相等，面板永久停在"进行中"且不再有任何事件。"""
    _, errors = normalize(
        [
            {"content": "a", "depends_on": ["t2"]},
            {"content": "b", "depends_on": ["t1"]},
        ]
    )
    assert any("dependency cycle" in e for e in errors)


def test_normalize_accepts_valid_dependency_chain() -> None:
    plan, errors = normalize(
        [
            {"content": "a"},
            {"content": "b", "depends_on": ["t1"]},
        ]
    )
    assert errors == []
    assert plan.steps[1].depends_on == ("t1",)


def test_to_todos_omits_empty_optional_fields() -> None:
    """可选字段只在非空时出现 —— 简单清单保持精简，协作信息不丢。"""
    plan, _ = normalize([{"content": "a"}])
    assert to_todos(plan) == [{"id": "t1", "content": "a", "status": "pending"}]

    plan2, _ = normalize(
        [{"content": "a", "owner": "run-1", "depends_on": [], "files": ["x.py"]}]
    )
    todo = to_todos(plan2)[0]
    assert todo["owner"] == "run-1"
    assert todo["files"] == ["x.py"]
    assert "depends_on" not in todo, "空的依赖列表不该出现在投影里"


def test_render_shows_owner_and_dependencies() -> None:
    """多 agent 共板时，模型必须能从回显看出"归谁、还在等谁"。"""
    plan, _ = normalize(
        [
            {"content": "a", "status": "completed"},
            {"content": "b", "owner": "run-1", "depends_on": ["t1"]},
        ]
    )
    text = render(plan)
    assert "@run-1" in text
    assert "← t1" in text


def test_normalize_invalid_status_collects_error() -> None:
    _, errors = normalize([{"content": "a", "status": "banana"}])
    assert any("invalid status" in e for e in errors)


def test_normalize_non_list() -> None:
    plan, errors = normalize("not a list")
    assert plan.steps == []
    assert errors


def test_normalize_empty_is_ok() -> None:
    plan, errors = normalize(None)
    assert plan.steps == []
    assert errors == []


def test_normalize_too_many_steps() -> None:
    _, errors = normalize([{"content": f"s{i}"} for i in range(60)])
    assert any("too many steps" in e for e in errors)


def test_validate_detects_stray_verified() -> None:
    plan = Plan(
        steps=[PlanStep(id="s1", content="x", verified_files=("a.py",), candidate_files=())]
    )
    assert validate(plan)


def test_render_marks_verified_with_check() -> None:
    plan = Plan(
        steps=[
            PlanStep(id="s1", content="edit", status="in_progress", candidate_files=("a.py",), verified_files=("a.py",)),
            PlanStep(id="s2", content="other"),
        ]
    )
    text = render(plan)
    assert "[~] edit ✔a.py" in text
    assert "[ ] other" in text


def test_to_todos_roundtrip_shape() -> None:
    plan = Plan(steps=[PlanStep(id="s1", content="x", status="completed")])
    assert to_todos(plan) == [{"id": "s1", "content": "x", "status": "completed"}]


def test_ordered_preserves_write_order() -> None:
    plan, _ = normalize([{"content": "first"}, {"content": "second"}, {"content": "third"}])
    assert [s.content for s in ordered(plan)] == ["first", "second", "third"]
