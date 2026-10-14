"""P2：guardian（工具执行前的 LLM 安全评审）单测。

重点钉住**安全底线**而非"happy path"：解析失败必须 fail-closed；且无论评审说什么，
delete / external 级都不许被放行。
"""

from __future__ import annotations

from app.agent.guardian import GuardianAssessment, may_release, parse_assessment


def test_parse_direct_json() -> None:
    a = parse_assessment('{"outcome":"allow","risk_level":"low","rationale":"reads a file"}')
    assert a.outcome == "allow"
    assert a.risk_level == "low"
    assert not a.failed


def test_parse_json_wrapped_in_prose() -> None:
    a = parse_assessment(
        'Sure, here is my verdict:\n{"outcome":"deny","risk_level":"high","rationale":"rm -rf"}\n'
    )
    assert a.outcome == "deny"
    assert a.risk_level == "high"
    assert not a.failed


def test_parse_unknown_outcome_defaults_deny() -> None:
    a = parse_assessment('{"outcome":"maybe","risk_level":"low","rationale":"?"}')
    assert a.outcome == "deny"
    assert not a.failed


def test_parse_empty_is_fail_closed() -> None:
    a = parse_assessment("")
    assert a.outcome == "deny"
    assert a.failed


def test_parse_non_json_is_fail_closed() -> None:
    a = parse_assessment("I think this is probably fine.")
    assert a.outcome == "deny"
    assert a.failed


def test_parse_malformed_json_is_fail_closed() -> None:
    a = parse_assessment('{"outcome": "allow", ')
    assert a.outcome == "deny"
    assert a.failed


def test_parse_json_array_is_fail_closed() -> None:
    a = parse_assessment("[1,2,3]")
    assert a.outcome == "deny"
    assert a.failed


def test_may_release_allows_write_level() -> None:
    a = GuardianAssessment("allow", "low", "safe")
    assert may_release("write", a) is True
    assert may_release("read", a) is True


def test_may_release_never_releases_delete_or_external() -> None:
    # 这是本模块的**安全底线**：不可逆 / 对外可见的操作始终要人，评审说了也不算。
    a = GuardianAssessment("allow", "low", "looks safe to me")
    assert may_release("delete", a) is False
    assert may_release("external", a) is False


def test_may_release_refuses_when_failed() -> None:
    a = GuardianAssessment("allow", "low", "x", failed=True)
    assert may_release("write", a) is False


def test_may_release_refuses_on_deny() -> None:
    a = GuardianAssessment("deny", "high", "no")
    assert may_release("write", a) is False


def test_may_release_unknown_level_is_denied() -> None:
    a = GuardianAssessment("allow", "low", "x")
    assert may_release("", a) is False
    assert may_release("weird", a) is False
