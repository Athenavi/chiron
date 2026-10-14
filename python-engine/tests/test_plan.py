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


def test_normalize_rejects_two_in_progress() -> None:
    _, errors = normalize(
        [
            {"content": "a", "status": "in_progress"},
            {"content": "b", "status": "in_progress"},
        ]
    )
    assert any("at most one step may be in_progress" in e for e in errors)


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
