"""M7c：介入待办端点（``GET /v1/agent/sessions/{sid}/interactions`` 与
``POST /v1/agent/interactions/{iid}/resolve``）的行为锁。

这里锁的是**答复形状由那一行的 kind 决定**这条契约 —— 它是整条闭环里最容易悄悄坏掉的一环：

* 形状存错了不报错，而是**过几小时才坏**：一张续跑取不出来的票会占住
  ``ux_pending_interactions_open`` 的名额，用户侧现象是"答了却没反应"；
* 所以端点必须**在写入前**按 kind 校验，并按 kind 归一成 `consume_grant` 认得的载荷
  （approval → `ApprovalDecision.to_json()`；question → `{answer}` / `{declined, reason}`）。

业务失败一律 200 + `error_code`（与网关 `interactionEngineError` 的映射成对），
这里同时锁住那串码值 —— 网关按字面量匹配，改一边就得改另一边。
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from app.agent import interactions
from app.agent.interactions import KIND_APPROVAL, KIND_QUESTION
from app.agent.runtime_types import ApprovalDecision
from app.api import interactions as api_mod


def _approval_row(**over: Any) -> dict[str, Any]:
    row = {
        "id": "i1",
        "kind": KIND_APPROVAL,
        "status": interactions.STATUS_PENDING,
        "session_id": "s1",
        "tool_name": "shell_exec",
        "args_hash": "abc",
        "request": {"kind": KIND_APPROVAL, "question": "要执行 rm 吗", "level": "write"},
    }
    row.update(over)
    return row


class FakeStore:
    """替掉 `interactions` 的三个读入口，并记录 `resolve` 收到了什么载荷。"""

    def __init__(self, *, row: dict[str, Any] | None = None, resolved: dict[str, Any] | None = None,
                 open_rows: list[dict[str, Any]] | None = None) -> None:
        self.row = row
        self.resolved = resolved
        self.open_rows = open_rows if open_rows is not None else []
        self.peek_calls: list[dict[str, str]] = []
        self.resolve_calls: list[dict[str, Any]] = []
        self.list_calls: list[dict[str, Any]] = []

    async def peek(self, *, interaction_id: str, tenant_id: str, user_id: str) -> dict[str, Any] | None:
        self.peek_calls.append({"id": interaction_id, "tenant": tenant_id, "user": user_id})
        return self.row

    async def resolve(self, **kw: Any) -> dict[str, Any] | None:
        self.resolve_calls.append(kw)
        return self.resolved

    async def list_open(self, **kw: Any) -> list[dict[str, Any]]:
        self.list_calls.append(kw)
        return self.open_rows


@pytest.fixture
def store(monkeypatch):
    """把 `interactions` 的三个读入口换成假存储，返回那个可断言的替身。"""

    def use(fake: FakeStore) -> FakeStore:
        monkeypatch.setattr(interactions, "peek", fake.peek)
        monkeypatch.setattr(interactions, "resolve", fake.resolve)
        monkeypatch.setattr(interactions, "list_open", fake.list_open)
        return fake

    return use


async def _resolve(iid: str = "i1", body: dict[str, Any] | None = None, **qs: Any) -> dict[str, Any]:
    # 缺省按**网关代理形态**给齐身份（网关总是用 claims 重写这两个 query）；
    # 只有专门测身份来源的用例才覆盖它们。
    kw: dict[str, Any] = {"tenant_id": "t1", "user_id": "u1"}
    kw.update(qs)
    return await api_mod.resolve_interaction(iid, body or {}, **kw)


# ── 读：待办列表 ──


async def test_list_requires_identity(store):  # noqa: ARG001
    fake = store(FakeStore())
    out = await api_mod.list_session_interactions("s1", tenant_id="t1", user_id="", limit=20)
    assert out["ok"] is False and out["error_code"] == "unauthorized"
    assert fake.list_calls == [], "身份缺失时不该读库"


async def test_list_returns_public_fields(store):
    fake = store(
        FakeStore(
            open_rows=[
                {
                    "id": "i1",
                    "kind": KIND_APPROVAL,
                    "status": "pending",
                    "tool_call_id": "c1",
                    "tool_name": "shell_exec",
                    "args_hash": "abc",
                    "request": {"question": "要执行吗"},
                    "decision": {},
                    "reason": "",
                    "created_at": "2026-10-18T00:00:00+00:00",
                    "expires_at": "2026-10-19T00:00:00+00:00",
                }
            ]
        )
    )
    out = await api_mod.list_session_interactions("s1", tenant_id="t1", user_id="u1", limit=20)

    assert out["ok"] is True and out["count"] == 1
    item = out["items"][0]
    assert item["request"] == {"question": "要执行吗"}
    # 内部指纹不属于待办契约：暴露它等于邀请客户端拿它当凭据
    assert "args_hash" not in item
    assert fake.list_calls[0]["user_id"] == "u1", "列表必须按来电者过滤，不能只靠会话 id"


# ── 写：答复 ──


async def test_approval_answer_is_stored_as_decision_json(store):
    fake = store(FakeStore(row=_approval_row(), resolved={"id": "i1", "kind": KIND_APPROVAL,
                                                         "session_id": "s1", "decision": {}}))
    out = await _resolve(body={"session_id": "s1", "response": {"decision": "approve"}},
                         tenant_id="t1", user_id="u1")

    assert out["ok"] is True
    assert fake.resolve_calls[0]["decision"] == {"decision": "approve"}
    assert fake.resolve_calls[0]["user_id"] == "u1"


async def test_approval_legacy_approved_bool_is_derived(store):
    """旧前端只发 `approved` —— 待办端点必须与实时审批端点同源（都走 parse_approval_payload）。"""
    fake = store(FakeStore(row=_approval_row(), resolved={"id": "i1", "decision": {}}))
    out = await _resolve(body={"session_id": "s1", "response": {"approved": False}})

    assert out["ok"] is True
    assert fake.resolve_calls[0]["decision"] == {"decision": "reject"}


async def test_approval_edit_stores_arguments_as_dict_for_the_resume_turn(store):
    """`edit` 的参数必须能被**续跑那一轮**原样读回（那是"批准的就是执行的"的落点）。"""
    fake = store(FakeStore(row=_approval_row(), resolved={"id": "i1", "decision": {}}))
    out = await _resolve(
        body={"session_id": "s1",
              "response": {"decision": "edit", "arguments": {"command": "ls -1"}, "reason": "改成只列目录"}}
    )

    assert out["ok"] is True
    payload = fake.resolve_calls[0]["decision"]
    assert payload["arguments"] == {"command": "ls -1"}
    parsed = ApprovalDecision.from_raw(json.dumps(payload))
    assert parsed is not None and parsed.decision == "edit"
    assert parsed.arguments == {"command": "ls -1"}
    assert parsed.reason == "改成只列目录"


async def test_question_answer_is_stored_as_answer_payload(store):
    fake = store(FakeStore(row=_approval_row(kind=KIND_QUESTION, tool_name="ask_user"),
                           resolved={"id": "i1", "decision": {}}))
    out = await _resolve(body={"session_id": "s1", "response": {"answer": "用生产库"}})

    assert out["ok"] is True
    assert fake.resolve_calls[0]["decision"] == {"answer": "用生产库"}


async def test_question_declined_is_visible_to_the_resume_turn(store):
    fake = store(FakeStore(row=_approval_row(kind=KIND_QUESTION, tool_name="ask_user"),
                           resolved={"id": "i1", "decision": {}}))
    out = await _resolve(body={"session_id": "s1", "response": {"declined": True, "reason": "我自己来"}})

    assert out["ok"] is True
    assert fake.resolve_calls[0]["decision"] == {"declined": True, "reason": "我自己来"}


async def test_wrong_shape_for_the_kind_is_rejected_before_writing(store):
    """把 approval 的三态发给一个 `question` 票 ⇒ 400，且**不写入**。

    这正是"存进去才坏"的那个坑：`_take_ask_grant` 只认 `answer`/`declined`，
    一张 `{decision: approve}` 的问答票等于用户答了但又没答。
    """
    fake = store(FakeStore(row=_approval_row(kind=KIND_QUESTION, tool_name="ask_user")))
    out = await _resolve(body={"session_id": "s1", "response": {"decision": "approve"}})

    assert out["error_code"] == "invalid_decision"
    assert fake.resolve_calls == []


async def test_unknown_decision_is_rejected_before_writing(store):
    fake = store(FakeStore(row=_approval_row()))
    out = await _resolve(body={"session_id": "s1", "response": {"decision": "maybe"}})

    assert out["error_code"] == "invalid_decision"
    assert fake.resolve_calls == []


async def test_session_mismatch_is_rejected(store):
    """声称的会话与票不同源 ⇒ 拒绝：否则网关唤醒的会话永远等不到那份答复。"""
    fake = store(FakeStore(row=_approval_row(session_id="s-other")))
    out = await _resolve(body={"session_id": "s1", "response": {"decision": "approve"}},
                         user_id="u1")

    assert out["error_code"] == "invalid_request"
    assert fake.resolve_calls == []


async def test_missing_session_id_is_rejected(store):  # noqa: ARG001
    out = await _resolve(body={"response": {"decision": "approve"}}, user_id="u1")
    assert out["error_code"] == "invalid_request"


async def test_empty_response_is_rejected(store):  # noqa: ARG001
    out = await _resolve(body={"session_id": "s1"}, user_id="u1")
    assert out["error_code"] == "invalid_request"


async def test_unknown_interaction_is_not_found(store):  # noqa: ARG001
    fake = store(FakeStore(row=None))
    out = await _resolve(body={"session_id": "s1", "response": {"decision": "approve"}}, user_id="u1")

    assert out["error_code"] == "not_found"
    assert fake.resolve_calls == []


async def test_already_answered_is_stale(store):  # noqa: ARG001
    fake = store(FakeStore(row=_approval_row(status=interactions.STATUS_ANSWERED)))
    out = await _resolve(body={"session_id": "s1", "response": {"decision": "approve"}}, user_id="u1")

    assert out["error_code"] == "stale"
    assert out["status"] == "answered"
    assert fake.resolve_calls == [], "已答的票不该被第二次写"


async def test_row_closed_between_read_and_write_is_stale(store):  # noqa: ARG001
    """实时通道刚好抢先收到答复（`pending → consumed`）：`resolve` 0 行命中 ⇒ stale。"""
    fake = store(FakeStore(row=_approval_row(), resolved=None))
    out = await _resolve(body={"session_id": "s1", "response": {"decision": "approve"}}, user_id="u1")

    assert out["error_code"] == "stale"
    assert len(fake.resolve_calls) == 1


async def test_unknown_kind_reports_storage_error(store):  # noqa: ARG001
    fake = store(FakeStore(row=_approval_row(kind="mystery")))
    out = await _resolve(body={"session_id": "s1", "response": {"decision": "approve"}}, user_id="u1")

    assert out["error_code"] == "storage_error"
    assert fake.resolve_calls == []


async def test_identity_may_come_from_the_body(store):  # noqa: ARG001
    """内部直连形态：query 不带身份时读 body —— 但仍然必须有，否则拒绝。"""
    fake = store(FakeStore(row=_approval_row(), resolved={"id": "i1", "decision": {}}))
    out = await _resolve(body={"session_id": "s1", "user_id": "u9", "tenant_id": "t9",
                               "response": {"decision": "approve"}},
                         tenant_id="", user_id="")
    assert out["ok"] is True
    assert fake.peek_calls[0] == {"id": "i1", "tenant": "t9", "user": "u9"}

    missing = await _resolve(body={"session_id": "s1", "response": {"decision": "approve"}},
                             tenant_id="", user_id="")
    assert missing["error_code"] == "unauthorized"


# ── 路由确实挂上了（网关按字面路径代理） ──


def test_routes_are_registered_on_the_api_router():
    from app.api import api_router

    paths = {getattr(route, "path", "") for route in api_router.routes}
    assert "/v1/agent/sessions/{session_id}/interactions" in paths
    assert "/v1/agent/interactions/{interaction_id}/resolve" in paths
