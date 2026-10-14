"""差距D：best-of-n（多候选 + judge 选优）单测。

钉住 fail-closed 的两处：候选非法整份拒绝；judge 选不出 winner 时不选、不改写。
"""

from __future__ import annotations

from app.agent.best_of_n import (
    DEFAULT_CRITERIA,
    Candidate,
    build_judge_prompt,
    parse_candidates,
    parse_judgment,
)


def test_parse_candidates_requires_at_least_two() -> None:
    _, errors = parse_candidates([{"content": "only one"}])
    assert errors


def test_parse_candidates_rejects_too_many() -> None:
    _, errors = parse_candidates([{"content": f"c{i}"} for i in range(8)])
    assert errors


def test_parse_candidates_rejects_empty_content() -> None:
    _, errors = parse_candidates([{"content": "a"}, {"content": "   "}])
    assert errors


def test_parse_candidates_autolabels() -> None:
    cands, errors = parse_candidates([{"content": "a"}, {"content": "b"}])
    assert errors == []
    assert [c.label for c in cands] == ["A", "B"]


def test_parse_candidates_keeps_explicit_label() -> None:
    cands, _ = parse_candidates([{"label": "x1", "content": "a"}, {"content": "b"}])
    assert cands[0].label == "x1"


def test_judge_prompt_lists_every_candidate_and_wraps_as_data() -> None:
    cands = [Candidate("A", "first"), Candidate("B", "second")]
    msgs = build_judge_prompt("do the task", "", cands)
    body = "\n".join(m["content"] for m in msgs)
    assert "candidate label=\"A\"" in body
    assert "candidate label=\"B\"" in body
    assert "do the task" in body
    # 未给判据时用默认序（而不是留空让模型自己发挥）。
    assert DEFAULT_CRITERIA.split(",")[0] in body
    # 候选内容必须被声明为**数据**。
    assert "untrusted DATA" in body


def test_parse_judgment_ok() -> None:
    j = parse_judgment('{"winner": "B", "scores": {"A": 0.3, "B": 0.9}, "rationale": "better"}', ["A", "B"])
    assert j.ok is True
    assert j.winner == "B"
    assert j.scores == {"A": 0.3, "B": 0.9}
    assert j.rationale == "better"


def test_parse_judgment_rejects_unknown_winner() -> None:
    j = parse_judgment('{"winner": "Z", "rationale": "x"}', ["A", "B"])
    assert j.ok is False
    assert j.winner == ""


def test_parse_judgment_rejects_non_json() -> None:
    j = parse_judgment("I prefer B.", ["A", "B"])
    assert j.ok is False


def test_parse_judgment_clamps_scores_and_ignores_unknown_labels() -> None:
    j = parse_judgment('{"winner": "A", "scores": {"A": 5, "Z": 1}}', ["A", "B"])
    assert j.ok is True
    assert j.scores["A"] == 1.0
    assert "Z" not in j.scores  # 不编造非候选的分数
