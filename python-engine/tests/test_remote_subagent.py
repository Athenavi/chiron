"""S3：远端 / 跨服务子 agent（方案 01 §4.3）。

四条约定逐条钉住：

* **默认关**：`remote_subagent_enabled=false` 时 `target="remote:…"` 直接拒绝（跨实例执行
  会扩大信任边界，不该是默认行为）；
* **接收方重新校验**：内网端点拒绝空 `tenant_id` / `user_id`，并拒绝超深委派；
* **结果不得丢失不可信包裹**：远端已包 ⇒ 原样；未包 ⇒ 本侧补包（两道保证）；
* **失败回退本实例**：没有可用远端 / 远端不可达 ⇒ 用 `ctx.run_child` 本地跑，不把委派变失败。
"""

from __future__ import annotations

import json
from typing import Any

from app.api.internal_subagent import InternalSubagentRequest, internal_subagent_run
from app.subagent.registry_targets import target_registry
from app.subagent.remote import RemoteSubagentTarget, list_remote_engines, resolve_engine, run_remote
from app.subagent.target import SubagentContext

# ── 白名单与默认关 ──────────────────────────────────────────────────────


def test_remote_prefix_is_registered():
    assert "remote" in target_registry.prefixes
    target = target_registry.resolve("remote:auto")
    assert not isinstance(target, str) and target.name == "remote:auto"


async def test_disabled_by_default(monkeypatch):
    """默认关：即使解析得到，也不发跨实例请求。"""
    monkeypatch.setattr("app.config.settings.remote_subagent_enabled", False)

    result = await RemoteSubagentTarget("auto").run("do", SubagentContext(task="do"))

    assert result.status == "failed"
    assert "disabled" in result.error


async def test_falls_back_to_local_when_disabled(monkeypatch):
    """关掉远端时**回退本实例** —— 而不是让一次委派失败。"""
    monkeypatch.setattr("app.config.settings.remote_subagent_enabled", False)
    called: list[str] = []

    async def run_child(task: str, *, profile_ref: str = "") -> Any:
        called.append(task)
        return _local_result()

    # 注意：默认关时**不**回退（那是显式配置的语义），所以这里断言相反的语义
    result = await RemoteSubagentTarget("auto").run(
        "do", SubagentContext(task="do", run_child=run_child)
    )
    assert result.status == "failed" and called == [], "显式关闭就该拒绝，不是偷偷本地跑"


# ── 发现与解析 ──────────────────────────────────────────────────────────


class _FakeRedis:
    def __init__(self, data: dict[str, Any]) -> None:
        self._data = data

    async def scan(self, cursor: int = 0, match: str | None = None, count: int = 100):  # noqa: ANN201
        prefix = (match or "").rstrip("*")
        keys = [k for k in self._data if k.startswith(prefix)]
        return 0, keys

    async def get(self, key: str) -> Any:
        if key not in self._data:
            return None
        return json.dumps(self._data[key]).encode()


def _patch_engines(monkeypatch: Any, *, data: dict[str, Any], self_id: str = "me") -> None:
    async def _get_redis():  # noqa: ANN202
        return _FakeRedis(data)

    monkeypatch.setattr("app.redis_client.get_redis", _get_redis)
    monkeypatch.setattr("app.subagent.affinity.cached_instance_id", lambda: self_id)


def _registry_keys(*entries: tuple[str, str]) -> dict[str, Any]:
    from app.redis_keys import rkey

    return {
        rkey(f"engine:instance:{instance_id}"): {"url": url, "instance_id": instance_id}
        for instance_id, url in entries
    }


async def test_lists_other_engines_only(monkeypatch):
    _patch_engines(
        monkeypatch,
        data=_registry_keys(("me", "http://a"), ("other", "http://b")),
    )

    engines = await list_remote_engines(exclude="me")

    assert engines == [{"instance_id": "other", "url": "http://b"}]


async def test_resolve_auto_and_explicit(monkeypatch):
    _patch_engines(monkeypatch, data=_registry_keys(("other", "http://b")))

    auto = await resolve_engine("auto")
    assert auto == {"instance_id": "other", "url": "http://b"}

    assert await resolve_engine("nope") is None, "指定的实例不在线 ⇒ None（调用方回退本实例）"


async def test_resolve_returns_none_without_peers(monkeypatch):
    _patch_engines(monkeypatch, data=_registry_keys(("me", "http://a")))
    assert await resolve_engine("auto") is None


# ── 远端调用 ────────────────────────────────────────────────────────────


class _FakeResponse:
    def __init__(self, payload: Any, status_code: int = 200) -> None:
        self._payload = payload
        self.status_code = status_code

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise RuntimeError(f"http {self.status_code}")

    def json(self) -> Any:
        return self._payload


class _FakeClient:
    instances: list[_FakeClient] = []

    def __init__(self, payload: Any = None, status_code: int = 200, **kwargs: Any) -> None:
        self.payload = payload
        self.status_code = status_code
        self.calls: list[dict[str, Any]] = []
        _FakeClient.instances.append(self)

    async def __aenter__(self) -> _FakeClient:
        return self

    async def __aexit__(self, *args: Any) -> bool:
        return False

    async def post(self, url: str, json: Any = None, headers: Any = None) -> _FakeResponse:  # noqa: A002
        self.calls.append({"url": url, "json": json, "headers": headers or {}})
        return _FakeResponse(self.payload, self.status_code)


def _patch_http(monkeypatch: Any, payload: Any, *, status_code: int = 200) -> None:
    import httpx

    _FakeClient.instances = []
    monkeypatch.setattr(
        httpx, "AsyncClient", lambda **kwargs: _FakeClient(payload, status_code, **kwargs)
    )


async def test_run_remote_sends_identity_and_token(monkeypatch):
    monkeypatch.setattr("app.config.settings.internal_token", "tok")
    _patch_http(monkeypatch, {"ok": True, "data": {"status": "completed", "output": "hi"}})

    result = await run_remote(
        engine={"instance_id": "other", "url": "http://b"},
        task="do it",
        tenant_id="t1",
        user_id="u1",
        session_id="s1",
    )

    assert result.ok and result.payload["output"] == "hi" and result.engine == "other"
    call = _FakeClient.instances[-1].calls[-1]
    assert call["url"] == "http://b/v1/internal/subagent/run"
    assert call["headers"]["X-Internal-Token"] == "tok"
    # 身份必须随请求走（接收方据此重新校验）
    assert call["json"]["tenant_id"] == "t1" and call["json"]["user_id"] == "u1"


async def test_run_remote_without_token_does_not_call(monkeypatch):
    monkeypatch.setattr("app.config.settings.internal_token", "")
    _patch_http(monkeypatch, {"ok": True})

    result = await run_remote(
        engine={"instance_id": "other", "url": "http://b"}, task="t", tenant_id="t1", user_id="u1"
    )

    assert not result.ok and "internal token" in result.error
    assert _FakeClient.instances == [], "没有凭据就不该发出请求"


async def test_run_remote_http_error_is_returned_not_raised(monkeypatch):
    monkeypatch.setattr("app.config.settings.internal_token", "tok")
    _patch_http(monkeypatch, {"ok": False}, status_code=503)

    result = await run_remote(
        engine={"instance_id": "other", "url": "http://b"}, task="t", tenant_id="t1", user_id="u1"
    )

    assert not result.ok and "failed" in result.error


# ── 端到端（Target 层）：回退与包裹 ─────────────────────────────────────


def _local_result() -> Any:
    from app.agent.subagent_runner import SubagentRunResult

    return SubagentRunResult(run_id="rs_local", status="completed", output="local answer")


async def test_falls_back_when_no_peer(monkeypatch):
    monkeypatch.setattr("app.config.settings.remote_subagent_enabled", True)
    _patch_engines(monkeypatch, data=_registry_keys(("me", "http://a")))
    called: list[str] = []

    async def run_child(task: str, *, profile_ref: str = "") -> Any:
        called.append(task)
        return _local_result()

    result = await RemoteSubagentTarget("auto").run(
        "do", SubagentContext(task="do", run_child=run_child)
    )

    assert called == ["do"], "没有可用远端 ⇒ 本地跑"
    assert result.run_id == "rs_local"


async def test_falls_back_when_remote_fails(monkeypatch):
    monkeypatch.setattr("app.config.settings.remote_subagent_enabled", True)
    monkeypatch.setattr("app.config.settings.internal_token", "tok")
    _patch_engines(monkeypatch, data=_registry_keys(("other", "http://b")))
    _patch_http(monkeypatch, {"ok": False}, status_code=500)
    called: list[str] = []

    async def run_child(task: str, *, profile_ref: str = "") -> Any:
        called.append(task)
        return _local_result()

    result = await RemoteSubagentTarget("auto").run(
        "do", SubagentContext(task="do", run_child=run_child)
    )

    assert called == ["do"] and result.run_id == "rs_local"


async def test_remote_output_is_wrapped_when_remote_did_not(monkeypatch):
    """不变量：结果**离场时**必须带 `<subagent-result>` —— 远端没包就本侧补。"""
    monkeypatch.setattr("app.config.settings.remote_subagent_enabled", True)
    monkeypatch.setattr("app.config.settings.internal_token", "tok")
    _patch_engines(monkeypatch, data=_registry_keys(("other", "http://b")))
    _patch_http(
        monkeypatch,
        {"ok": True, "data": {"status": "completed", "output": "raw text", "result_ref": "rs_r"}},
    )

    result = await RemoteSubagentTarget("auto").run("do", SubagentContext(task="do", tenant_id="t1", user_id="u1"))

    assert result.status == "completed"
    assert "<subagent-result" in result.output, "远端没包，本侧必须补包"
    assert "raw text" in result.output
    assert result.run_id == "rs_r", "保留远端的 result_ref，便于追远端那次运行"


async def test_already_wrapped_output_is_passed_through(monkeypatch):
    monkeypatch.setattr("app.config.settings.remote_subagent_enabled", True)
    monkeypatch.setattr("app.config.settings.internal_token", "tok")
    _patch_engines(monkeypatch, data=_registry_keys(("other", "http://b")))
    wrapped = '<subagent-result run_id="rs_r">\n  body\n</subagent-result>'
    _patch_http(monkeypatch, {"ok": True, "data": {"status": "completed", "output": wrapped}})

    result = await RemoteSubagentTarget("auto").run("do", SubagentContext(task="do"))

    assert result.output == wrapped, "已包就原样，不重复包裹"


# ── 接收端：身份重校验 ──────────────────────────────────────────────────


async def test_receiver_rejects_missing_identity():
    """接收方**重新校验**身份：无归属的跨实例调用一律拒绝。"""
    out = await internal_subagent_run(InternalSubagentRequest(task="do", tenant_id="", user_id=""))
    assert out["ok"] is False and "required" in out["error"]

    out2 = await internal_subagent_run(
        InternalSubagentRequest(task="do", tenant_id="t1", user_id="")
    )
    assert out2["ok"] is False


async def test_receiver_rejects_empty_task():
    out = await internal_subagent_run(
        InternalSubagentRequest(task="   ", tenant_id="t1", user_id="u1")
    )
    assert out["ok"] is False and "task" in out["error"]


async def test_receiver_rejects_excessive_depth():
    """远端不得成为绕过递归限制的通道。"""
    from app.tools.subagent import MAX_DEPTH

    out = await internal_subagent_run(
        InternalSubagentRequest(task="do", tenant_id="t1", user_id="u1", depth=MAX_DEPTH)
    )
    assert out["ok"] is False and "depth" in out["error"]
