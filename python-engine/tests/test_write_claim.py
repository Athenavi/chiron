"""P4：写占用裁决（app/agent/write_claim.py）单测。

钉住语义：同一 owner 可重复写（续期）；**跨 owner** 才冲突；TTL 过期可被接管。
"""

from __future__ import annotations

import pytest

from app.agent import write_claim


@pytest.fixture(autouse=True)
def _clean() -> None:
    write_claim.reset()
    yield
    write_claim.reset()


def test_claim_then_conflict_from_other_owner() -> None:
    ok, holder = write_claim.claim("a.py", "agent-1")
    assert ok and holder == ""
    ok, holder = write_claim.claim("a.py", "agent-2")
    assert ok is False
    assert holder == "agent-1"


def test_same_owner_can_reclaim() -> None:
    assert write_claim.claim("a.py", "agent-1")[0]
    # 同一个 owner 再写同一个文件是正常的（它在迭代自己的文件）。
    assert write_claim.claim("a.py", "agent-1")[0]


def test_different_files_do_not_conflict() -> None:
    assert write_claim.claim("a.py", "agent-1")[0]
    assert write_claim.claim("b.py", "agent-2")[0]


def test_release_only_own() -> None:
    write_claim.claim("a.py", "agent-1")
    write_claim.release("a.py", "agent-2")  # 不是它的
    assert write_claim.claim("a.py", "agent-2")[0] is False
    write_claim.release("a.py", "agent-1")
    assert write_claim.claim("a.py", "agent-2")[0] is True


def test_release_all_frees_everything_for_owner() -> None:
    write_claim.claim("a.py", "agent-1")
    write_claim.claim("b.py", "agent-1")
    write_claim.claim("c.py", "agent-2")
    assert write_claim.release_all("agent-1") == 2
    assert write_claim.claim("a.py", "agent-3")[0] is True
    assert write_claim.claim("c.py", "agent-3")[0] is False


def test_expired_claim_can_be_taken(monkeypatch: pytest.MonkeyPatch) -> None:
    clock = {"t": 1000.0}
    monkeypatch.setattr(write_claim.time, "time", lambda: clock["t"])
    assert write_claim.claim("a.py", "agent-1", ttl=10.0)[0]
    clock["t"] += 11.0
    assert write_claim.claim("a.py", "agent-2")[0] is True


def test_empty_path_or_owner_is_not_adjudicated() -> None:
    # 缺 path / owner 不做裁决（交给下游参数校验），绝不因此拦下正常调用。
    assert write_claim.claim("", "agent-1")[0] is True
    assert write_claim.claim("a.py", "")[0] is True


def test_path_of_extracts_from_write_tools() -> None:
    assert write_claim.path_of("write_file", {"path": "a.go"}) == "a.go"
    assert write_claim.path_of("edit_file", {"file_path": "b.go"}) == "b.go"


def test_path_of_ignores_non_write_tools() -> None:
    assert write_claim.path_of("read_file", {"path": "a.go"}) == ""
    assert write_claim.path_of("shell_exec", {"command": "rm x"}) == ""
