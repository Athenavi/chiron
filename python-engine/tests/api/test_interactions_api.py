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
from app.agent.interactions import KIND_APPROVAL, KIND_BUDGET, KIND_QUESTION
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


def _budget_row(**over: Any) -> dict[str, Any]:
    row = {
        "id": "b1",
        "kind": KIND_BUDGET,
        "status": interactions.STATUS_PENDING,
        "session_id": "s-main",
        "tool_name": "subagent",
        "args_hash": "deadbeef",
        "request": {
            "root_run_id": "rs_root",
            "axis": "line_tokens",
            "cap_tokens": 1000,
            "settled_tokens": 1000,
            "suggested_cap_tokens": 2000,
        },
    }
    row.update(over)
    return row


class FakeLedger:
    """替掉线账的写侧：提额是这条分支唯一真正的副作用，必须能断言它收到了什么。"""

    def __init__(self, *, ok: bool = True) -> None:
        self.ok = ok
        self.calls: list[tuple[Any, ...]] = []

    async def raise_cap_and_resume(
        self, root: str, tenant_id: int, *, cap_tokens: int, updated_by: str
    ) -> bool:
        self.calls.append((root, tenant_id, cap_tokens, updated_by))
        return self.ok


class FakeStore:
    """替掉 `interactions` 的读写入口，并记录 `resolve` / `settle` 收到了什么载荷。"""

    def __init__(
        self,
        *,
        row: dict[str, Any] | None = None,
        resolved: dict[str, Any] | None = None,
        open_rows: list[dict[str, Any]] | None = None,
        settle_ok: bool = True,
    ) -> None:
        self.row = row
        self.resolved = resolved
        self.open_rows = open_rows if open_rows is not None else []
        self.settle_ok = settle_ok
        self.peek_calls: list[dict[str, str]] = []
        self.resolve_calls: list[dict[str, Any]] = []
        self.settle_calls: list[dict[str, Any]] = []
        self.list_calls: list[dict[str, Any]] = []

    async def peek(self, *, interaction_id: str, tenant_id: int, user_id: str) -> dict[str, Any] | None:
        self.peek_calls.append({"id": interaction_id, "tenant": tenant_id, "user": user_id})
        return self.row

    async def resolve(self, **kw: Any) -> dict[str, Any] | None:
        self.resolve_calls.append(kw)
        return self.resolved

    async def settle(self, **kw: Any) -> bool:
        self.settle_calls.append(kw)
        return self.settle_ok

    async def list_open(self, **kw: Any) -> list[dict[str, Any]]:
        self.list_calls.append(kw)
        return self.open_rows


@pytest.fixture
def store(monkeypatch):
    """把 `interactions` 的读写入口换成假存储，返回那个可断言的替身。"""

    def use(fake: FakeStore) -> FakeStore:
        monkeypatch.setattr(interactions, "peek", fake.peek)
        monkeypatch.setattr(interactions, "resolve", fake.resolve)
        monkeypatch.setattr(interactions, "settle", fake.settle)
        monkeypatch.setattr(interactions, "list_open", fake.list_open)
        return fake

    return use


@pytest.fixture
def ledger(monkeypatch):
    from app.subagent import line_ledger as ledger_mod

    def use(fake: FakeLedger) -> FakeLedger:
        monkeypatch.setattr(ledger_mod, "get_line_ledger", lambda: fake)
        return fake

    return use


async def _resolve(iid: str = "i1", body: dict[str, Any] | None = None, **qs: Any) -> dict[str, Any]:
    # 缺省按**网关代理形态**给齐身份（网关总是用 claims 重写这两个 query）；
    # 只有专门测身份来源的用例才覆盖它们。
    kw: dict[str, Any] = {"tenant_id": 1, "user_id": "u1"}
    kw.update(qs)
    return await api_mod.resolve_interaction(iid, body or {}, **kw)


# ── 读：待办列表 ──


async def test_list_requires_identity(store):  # noqa: ARG001
    fake = store(FakeStore())
    out = await api_mod.list_session_interactions("s1", tenant_id=1, user_id="", limit=20)
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
    out = await api_mod.list_session_interactions("s1", tenant_id=1, user_id="u1", limit=20)

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
                         tenant_id=1, user_id="u1")

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
    out = await _resolve(body={"session_id": "s1", "user_id": "u9", "tenant_id": 9,
                               "response": {"decision": "approve"}},
                         tenant_id="", user_id="")
    assert out["ok"] is True
    assert fake.peek_calls[0] == {"id": "i1", "tenant": 9, "user": "u9"}

    missing = await _resolve(body={"session_id": "s1", "response": {"decision": "approve"}},
                             tenant_id="", user_id="")
    assert missing["error_code"] == "unauthorized"


# ── M15c：提额票（`kind=budget`）走的是写控制态，不是一张待取的票 ──


async def test_budget_reply_raises_the_cap_and_closes_the_ticket(store, ledger):  # noqa: ARG001
    fake = store(FakeStore(row=_budget_row()))
    led = ledger(FakeLedger())
    out = await _resolve(body={"session_id": "s-main", "response": {"cap_tokens": 5000}})

    assert led.calls == [("rs_root", 1, 5000, "u1")], "上限与清暂停必须一次写完成"
    assert out["ok"] is True and out["data"]["status"] == interactions.STATUS_CONSUMED
    assert fake.settle_calls[0]["decision"] == {"cap_tokens": 5000, "root_run_id": "rs_root"}
    # 关掉票时带上身份：`settle` 本身可以按 id 关任何人的票，这条分支不行
    assert fake.settle_calls[0]["tenant_id"] == 1
    assert fake.settle_calls[0]["user_id"] == "u1"
    # **不走 `resolve`**：停在 `answered` 的票会永久占住 `ux_pending_interactions_open` 的名额，
    # 下次再耗尽时新票创建不出来 ⇒ "线停了却没有待办"。
    assert fake.resolve_calls == []


@pytest.mark.parametrize(
    "response",
    [
        {"cap_tokens": "5000"},
        {"cap_tokens": True},
        {"cap_tokens": 1.5},
        {"cap_tokens": 0},
        {"cap_tokens": -1},
        {"cap_tokens": 10**12 + 1},
    ],
)
async def test_budget_shape_is_checked_before_any_write(store, ledger, response):  # noqa: ARG001
    """写错了不该"先存着"：那等于告诉用户提了额，而下一条段照样被拦。"""
    fake = store(FakeStore(row=_budget_row()))
    led = ledger(FakeLedger())
    out = await _resolve(body={"session_id": "s-main", "response": response})

    assert out["error_code"] == "invalid_decision", response
    assert led.calls == [] and fake.settle_calls == []


async def test_budget_write_failure_keeps_the_ticket_open(store, ledger):  # noqa: ARG001
    """提额没写进库 ⇒ 如实报错、票保持 `pending`。返回 ok 是说谎：用户会以为已经提上去了。"""
    fake = store(FakeStore(row=_budget_row()))
    led = ledger(FakeLedger(ok=False))
    out = await _resolve(body={"session_id": "s-main", "response": {"cap_tokens": 5000}})

    assert out["ok"] is False and out["error_code"] == "storage_error"
    assert len(led.calls) == 1 and fake.settle_calls == []


async def test_budget_reply_is_ok_even_when_the_ticket_was_already_closed(store, ledger):  # noqa: ARG001
    """关票失败（被巡检抢先转成 `expired`）不推翻已经生效的提额 —— 告诉他失败只会让他再点一次。"""
    fake = store(FakeStore(row=_budget_row(), settle_ok=False))
    ledger(FakeLedger())
    out = await _resolve(body={"session_id": "s-main", "response": {"cap_tokens": 5000}})

    assert out["ok"] is True
    assert len(fake.settle_calls) == 1, "确实试过关票，只是没关掉"
    assert out["data"]["status"] == interactions.STATUS_PENDING, "如实回显票此刻的状态"


async def test_budget_needs_a_tenant(store, ledger):  # noqa: ARG001
    """线账按租户归属过滤：没有 tenant_id 就没法安全地写那一行 ⇒ 宁可拒绝。"""
    fake = store(FakeStore(row=_budget_row()))
    led = ledger(FakeLedger())
    out = await _resolve(
        body={"session_id": "s-main", "response": {"cap_tokens": 5000}}, tenant_id=""
    )

    assert out["error_code"] == "invalid_request"
    assert led.calls == [] and fake.settle_calls == []


async def test_budget_ticket_without_a_line_root_is_a_storage_error(store, ledger):  # noqa: ARG001
    """票上的 `root_run_id` 是写入目标；缺了它 = 写入方与读取方对同一张表的理解漂移了。"""
    fake = store(FakeStore(row=_budget_row(request={"axis": "line_tokens"})))
    led = ledger(FakeLedger())
    out = await _resolve(body={"session_id": "s-main", "response": {"cap_tokens": 5000}})

    assert out["error_code"] == "storage_error"
    assert led.calls == [] and fake.settle_calls == []


# ── 路由确实挂上了（网关按字面路径代理） ──


def test_routes_are_registered_on_the_api_router():
    from fastapi import FastAPI

    from app.api import api_router

    # ⚠ 用 **`openapi()` 的 paths**，而不是 `api_router.routes`：后者的**形状随 FastAPI 版本变**
    # —— `requirements.txt` 钉 `fastapi==0.143.0`（CI 装的就是它），该版本把 `include_router`
    # 改成惰性的 `_IncludedRouter` 占位对象（**没有 `path` 属性**），于是 `routes` 上根本看不到
    # 真实路径；本机 0.136.3 还是展平的，所以这条只在 CI 红。`openapi()` 是**公共契约**，
    # 跨版本稳定，而且它同时证明"路由真的可路由"，比看内部结构更有意义。
    app = FastAPI()
    app.include_router(api_router)
    paths = set(app.openapi().get("paths", {}))
    # ⚠ CI（Linux/3.11）上这条曾经红过：`paths == {''}` —— 不是"集合为空"，而是**恰好一个
    # 取不到 `path` 的对象**。本机（Windows/3.14）跑同一命令是绿的 ⇒ 光看断言只能知道
    # "路由没挂上"，不知道挂成了什么。所以在断言**之前**把实际内容打出来。
    if len(paths) <= 1:
        print(f"[routes] n={len(api_router.routes)} paths={sorted(paths)}")
        for route in api_router.routes:
            print(
                f"[routes] {type(route).__module__}.{type(route).__name__} "
                f"path={getattr(route, 'path', '<无 path 属性>')!r}"
            )
    assert "/v1/agent/sessions/{session_id}/interactions" in paths
    assert "/v1/agent/interactions/{interaction_id}/resolve" in paths
