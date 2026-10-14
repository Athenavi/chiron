"""P5：失败分类与恢复路由（app/agent/recovery.py）单测。

钉住：只读放行、无失败放行、换操作放行、**同一失败操作**在预算耗尽后判 REVIEW。
"""

from __future__ import annotations

from app.agent import recovery
from app.agent.recovery import Action, Failure, FailureKind, RecoveryState


def _fail(tool: str, args: dict, level: str = "write") -> Failure:
    return Failure(
        fingerprint=recovery.fingerprint(tool, args),
        tool_name=tool,
        level=level,
        reason="prev failure",
    )


def test_readonly_always_allowed() -> None:
    st = RecoveryState()
    st.record_failure(_fail("read_file", {"path": "a"}, level="read"))
    d = recovery.decide(tool_name="read_file", level="read", args={"path": "a"}, state=st)
    assert d.action == Action.ALLOW
    assert d.kind == FailureKind.NONE


def test_no_active_failure_allows() -> None:
    d = recovery.decide(
        tool_name="write_file", level="write", args={"path": "a"}, state=RecoveryState()
    )
    assert d.action == Action.ALLOW
    assert d.kind == FailureKind.NONE


def test_different_operation_allows() -> None:
    st = RecoveryState()
    st.record_failure(_fail("shell_exec", {"command": "go build"}))
    d = recovery.decide(
        tool_name="shell_exec", level="write", args={"command": "go test"}, state=st
    )
    assert d.action == Action.ALLOW
    assert d.kind == FailureKind.DIFFERENT_OPERATION


def test_same_operation_first_retry_allowed_then_review() -> None:
    st = RecoveryState()
    args = {"path": "a.py", "content": "x"}
    # 第一次失败
    st.record_failure(_fail("write_file", args))
    # 同一个操作、预算还有：放行
    d1 = recovery.decide(tool_name="write_file", level="write", args=args, state=st)
    assert d1.action == Action.ALLOW
    assert d1.kind == FailureKind.SAME_OPERATION
    # 重试又失败 → 记录（消耗预算）
    st.record_failure(_fail("write_file", args))
    # 第三次同一个操作：预算耗尽 → REVIEW
    d2 = recovery.decide(tool_name="write_file", level="write", args=args, state=st)
    assert d2.action == Action.REVIEW
    assert d2.kind == FailureKind.SAME_OPERATION


def test_success_clears_failure() -> None:
    st = RecoveryState()
    st.record_failure(_fail("write_file", {"path": "a"}))
    st.record_success()
    d = recovery.decide(
        tool_name="write_file", level="write", args={"path": "a"}, state=st
    )
    assert d.action == Action.ALLOW
    assert d.kind == FailureKind.NONE


def test_fingerprint_ignores_key_order() -> None:
    a = recovery.fingerprint("write_file", {"path": "a", "content": "x"})
    b = recovery.fingerprint("write_file", {"content": "x", "path": "a"})
    assert a == b


def test_fingerprint_differs_by_value() -> None:
    a = recovery.fingerprint("write_file", {"path": "a"})
    b = recovery.fingerprint("write_file", {"path": "b"})
    assert a != b


def test_review_not_repeated_forever() -> None:
    # REVIEW 之后仍判 REVIEW（要求换策略），且 reviewed 集合记录了这个指纹。
    st = RecoveryState()
    args = {"path": "a"}
    st.record_failure(_fail("write_file", args))
    st.retries_used = recovery.SAFE_RETRY_BUDGET
    d1 = recovery.decide(tool_name="write_file", level="write", args=args, state=st)
    d2 = recovery.decide(tool_name="write_file", level="write", args=args, state=st)
    assert d1.action == Action.REVIEW
    assert d2.action == Action.REVIEW
    assert recovery.fingerprint("write_file", args) in st.reviewed
