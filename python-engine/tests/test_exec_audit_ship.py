"""执行审计的**多副本集中摄取**（N4）：引擎把 exec_audit 记录成批送到网关。

为什么值得单独测：这条链路有三条容易写错的纪律 ——
① **默认关**（没配 URL 就零行为变化）；② **不阻断执行**（发送失败只告警，绝不抛给调用方）；
③ 字段映射（落盘用 `tenant/user/session`，网关契约用 `tenant_id/user_id/session_id`）。
另外队列是**有界**的：满了丢最旧且必须计数，否则"审计静默丢失"没人知道。
"""
from __future__ import annotations

import collections

import httpx
import pytest

from app.tools import exec_audit


@pytest.fixture(autouse=True)
def _isolate_ship_state(monkeypatch, tmp_path):
    """包级状态（队列/计数/任务）+ 审计目录都要隔离并还原（否则污染同进程其它用例）。"""
    monkeypatch.setattr(exec_audit, "AUDIT_DIR", tmp_path)
    monkeypatch.delenv(exec_audit.SHIP_URL_ENV, raising=False)
    snapshot_queue = collections.deque(exec_audit._ship_queue, maxlen=exec_audit._ship_queue.maxlen)
    snapshot_stats = dict(exec_audit.SHIP_STATS)
    # 后台任务替换成空实现：让用例用 flush_ship_queue 确定性地发货，避免与后台任务抢批
    monkeypatch.setattr(exec_audit, "_ship_loop", _noop)
    yield
    exec_audit._ship_queue.clear()
    exec_audit._ship_queue.extend(snapshot_queue)
    exec_audit.SHIP_STATS.clear()
    exec_audit.SHIP_STATS.update(snapshot_stats)


async def _noop() -> None:  # pragma: no cover - 仅用于让 _enqueue 不真的起后台发货
    return None


def _record(tool: str = "shell_exec", outcome: str = "ok") -> None:
    exec_audit.record_execution(
        tool=tool, command="echo hi", outcome=outcome, exit_code=0, duration_ms=12
    )


def _patch_transport(monkeypatch, handler) -> list[httpx.Request]:
    captured: list[httpx.Request] = []

    def _handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return handler(request)

    real_client = httpx.AsyncClient
    transport = httpx.MockTransport(_handler)
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: real_client(transport=transport, **kw))
    return captured


def test_disabled_by_default(monkeypatch):
    """没配 URL ⇒ 不入队、不发货，只有落盘（零行为变化）。"""
    assert exec_audit.ship_enabled() is False
    _record()
    assert exec_audit.SHIP_STATS["enqueued"] == 0
    assert len(exec_audit._ship_queue) == 0
    assert (exec_audit._audit_dir() / exec_audit.EXEC_AUDIT_FILENAME).exists()


@pytest.mark.asyncio
async def test_ships_batch_with_token_and_field_mapping(monkeypatch):
    monkeypatch.setenv(exec_audit.SHIP_URL_ENV, "http://gateway:8080/v1/internal/audit/exec")
    monkeypatch.setattr("app.config.settings.internal_token", "tk-drill", raising=False)
    captured = _patch_transport(monkeypatch, lambda _r: httpx.Response(200, json={"accepted": 1}))

    from app.tools.context import set_tool_context

    set_tool_context(tenant_id="t1", user_id="u1", session_id="s1")
    _record()
    assert exec_audit.SHIP_STATS["enqueued"] == 1

    sent = await exec_audit.flush_ship_queue()
    assert sent == 1 and len(captured) == 1

    request = captured[0]
    assert str(request.url) == "http://gateway:8080/v1/internal/audit/exec"
    assert request.headers["X-Internal-Token"] == "tk-drill"
    import json

    records = json.loads(request.content)["records"]
    assert len(records) == 1
    rec = records[0]
    # 字段映射：落盘名 → 网关契约名
    assert (rec["tenant_id"], rec["user_id"], rec["session_id"]) == ("t1", "u1", "s1")
    assert rec["tool"] == "shell_exec" and rec["outcome"] == "ok"
    assert rec["exit_code"] == 0 and rec["duration_ms"] == 12
    assert rec["instance"], "必须带来源实例（否则集中之后分不清是谁发的）"
    assert rec["ts"]


@pytest.mark.asyncio
async def test_ship_failure_is_swallowed_and_counted(monkeypatch):
    """网关 500 / 传输异常都不得抛给调用方，只计数 + 告警。"""
    monkeypatch.setenv(exec_audit.SHIP_URL_ENV, "http://gateway:8080/v1/internal/audit/exec")
    _patch_transport(monkeypatch, lambda _r: httpx.Response(500, text="boom"))
    _record()
    before = exec_audit.SHIP_STATS["dropped"]
    assert await exec_audit.flush_ship_queue() == 0
    assert exec_audit.SHIP_STATS["dropped"] == before + 1

    def _explode(_request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("no route to host")

    _patch_transport(monkeypatch, _explode)
    _record()
    assert await exec_audit.flush_ship_queue() == 0  # 不抛异常


def test_queue_is_bounded_and_counts_drops(monkeypatch):
    """队列有界：满了丢最旧，且必须计数（否则静默丢审计）。"""
    monkeypatch.setenv(exec_audit.SHIP_URL_ENV, "http://gateway:8080/v1/internal/audit/exec")
    monkeypatch.setattr(exec_audit, "_ship_queue", collections.deque(maxlen=3))
    before = exec_audit.SHIP_STATS["dropped"]
    for i in range(5):
        exec_audit._enqueue_for_shipping({"tool": "shell_exec", "outcome": "ok", "command": f"c{i}"})
    assert len(exec_audit._ship_queue) == 3
    assert exec_audit.SHIP_STATS["dropped"] == before + 2
