"""批 G+ 批次 3：`webhook` 形态（出站 POST 到运维声明的 URL）回归。

对应 `docs/hook-protocol-design.md` §7 的验收 ③/④/⑤/⑦ 与批次 3 独有的两条：
- **闸口 fail-closed**：独立开关默认关 + host allowlist 默认空 ⇒ 一个目标也不批；
- **载荷内容无关**：不送 `tool_arguments` / `tool_result`，也不送租户身份（踩过 §6 的"数据出境"红线）。

出站**真的发包**需要外部服务，本机与 CI 都不保证；因此这里用两层取证：
① 闸门与判定语义用**假 httpx 客户端**验证（请求形状、响应解释、超时、体积）；
② SSRF 那一层用**真的 `assert_safe_url`**（不 mock）验证内网目标被拦 —— 那是本批最该被钉住的一条。
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from app.config import settings
from app.hooks import HANDLER_WEBHOOK, OWNER_USER, events, hooks
from app.hooks import config as hooks_config
from app.hooks.runner import webhook_payload, webhook_target_error

#: 通配用的假 host —— 不解析、不联网（`assert_safe_url` 在多数用例里被替换）。
FAKE_HOST = "hooks.example.com"
FAKE_URL = f"https://{FAKE_HOST}/v1/hook"


class _FakeResponse:
    def __init__(self, status_code: int, body: bytes = b"") -> None:
        self.status_code = status_code
        self._body = body

    async def aiter_bytes(self) -> Any:
        yield self._body


class _FakeStream:
    def __init__(self, response: _FakeResponse) -> None:
        self._response = response

    async def __aenter__(self) -> _FakeResponse:
        return self._response

    async def __aexit__(self, *exc: Any) -> bool:
        return False


class _FakeClient:
    """记录请求、返回预设响应的 httpx.AsyncClient 替身。"""

    instances: list[_FakeClient] = []
    response: _FakeResponse | None = None
    raise_exc: Exception | None = None

    def __init__(self, **kwargs: Any) -> None:
        self.kwargs = kwargs
        self.requests: list[tuple[str, str, dict[str, Any]]] = []
        _FakeClient.instances.append(self)

    async def __aenter__(self) -> _FakeClient:
        return self

    async def __aexit__(self, *exc: Any) -> bool:
        return False

    def stream(self, method: str, url: str, **kwargs: Any) -> _FakeStream:
        self.requests.append((method, url, kwargs.get("json", {})))
        if _FakeClient.raise_exc is not None:
            raise _FakeClient.raise_exc
        assert _FakeClient.response is not None, "用例必须先设置 _FakeClient.response"
        return _FakeStream(_FakeClient.response)


def _set_response(status: int, body: Any = b"") -> None:
    payload = json.dumps(body).encode("utf-8") if isinstance(body, (dict, list)) else body
    _FakeClient.response = _FakeResponse(status, payload)


@pytest.fixture(autouse=True)
def _hooks_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    from app.hooks import audit

    monkeypatch.setattr(audit, "AUDIT_DIR", tmp_path)
    monkeypatch.setattr(settings, "hooks_enabled", False)
    monkeypatch.setattr(settings, "hooks_allow_user_defined", False)
    monkeypatch.setattr(settings, "hooks_timeout_seconds", 5)
    monkeypatch.setattr(settings, "hooks_event_budget_seconds", 10)
    monkeypatch.setattr(settings, "hooks_config_path", "")
    monkeypatch.setattr(settings, "hooks_allow_webhooks", False)
    monkeypatch.setattr(settings, "hooks_webhook_allowlist", "")
    monkeypatch.setattr(settings, "hooks_allow_context_injection", False)
    hooks.clear()
    yield
    hooks.clear()


@pytest.fixture
def fake_http(monkeypatch: pytest.MonkeyPatch) -> type[_FakeClient]:
    """替换 httpx 客户端与 SSRF 守卫（只保留"闸门/判定"这一层被测）。"""
    import httpx

    from app.tools import ssrf

    monkeypatch.setattr(ssrf, "assert_safe_url", lambda url: None)
    monkeypatch.setattr(httpx, "AsyncClient", _FakeClient)
    _FakeClient.instances = []
    _FakeClient.response = None
    _FakeClient.raise_exc = None
    return _FakeClient


def _task(**overrides: Any) -> SimpleNamespace:
    base = {"tenant_id": "t_wh", "user_id": "u_wh", "session_id": "s_wh"}
    base.update(overrides)
    return SimpleNamespace(**base)


def _call(name: str = "shell_exec", arguments: str = '{"cmd": "ls"}') -> dict[str, Any]:
    return {"id": "call_1", "name": name, "arguments": arguments}


def _entries(root: Path) -> list[dict[str, Any]]:
    path = root / "hooks_audit.jsonl"
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]


def _enable(monkeypatch: pytest.MonkeyPatch, *, allowlist: str = "", config_path: str = "") -> None:
    monkeypatch.setattr(settings, "hooks_enabled", True)
    monkeypatch.setattr(settings, "hooks_allow_webhooks", True)
    monkeypatch.setattr(settings, "hooks_webhook_allowlist", allowlist or FAKE_HOST)
    monkeypatch.setattr(settings, "hooks_config_path", config_path)


def _register(name: str = "ops_webhook", url: str = FAKE_URL) -> bool:
    return hooks.register(events.PRE_TOOL_USE, name, handler=HANDLER_WEBHOOK, url=url)


# ── ① 闸口 ───────────────────────────────────────────────────────────────────


def test_switch_off_denies_registration(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "hooks_webhook_allowlist", FAKE_HOST)

    assert _register() is False
    assert hooks.registry.all() == []
    last = _entries(tmp_path)[-1]
    assert last["outcome"] == "register_denied"
    assert "hooks_allow_webhooks" in last["reason"]


def test_empty_allowlist_is_fail_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "hooks_allow_webhooks", True)
    monkeypatch.setattr(settings, "hooks_webhook_allowlist", "")

    assert _register() is False
    assert "allowlist" in (webhook_target_error(FAKE_URL) or "")


def test_host_outside_allowlist_is_denied(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "hooks_allow_webhooks", True)
    monkeypatch.setattr(settings, "hooks_webhook_allowlist", "other.example.com")

    assert _register() is False
    assert "not in hooks_webhook_allowlist" in (webhook_target_error(FAKE_URL) or "")


def test_invalid_url_is_denied(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "hooks_allow_webhooks", True)
    monkeypatch.setattr(settings, "hooks_webhook_allowlist", FAKE_HOST)

    assert webhook_target_error("not-a-url") is not None
    assert webhook_target_error("") is not None


def test_registration_does_not_touch_the_network(monkeypatch: pytest.MonkeyPatch) -> None:
    """注册只判**静态**事实（开关 + host allowlist）：SSRF 解析属运行时，不该在启动时发生。"""
    from app.tools import ssrf

    def _explode(url: str) -> None:
        raise AssertionError("registration must not resolve DNS")

    monkeypatch.setattr(ssrf, "assert_safe_url", _explode)
    _enable(monkeypatch)

    assert _register() is True


# ── ② SSRF（用真的守卫，不 mock）────────────────────────────────────────────


async def test_ssrf_check_blocks_internal_target(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """内网/云元数据目标必须被**执行时**的 SSRF 守卫拦下（这是本批最该钉住的一条）。"""
    _enable(monkeypatch, allowlist="127.0.0.1")
    assert _register(url="http://127.0.0.1:8080/hook") is True

    blocked = await hooks.before_tool_use(task=_task(), tool_call=_call())

    assert blocked is None, "被拦下的是**出站**，不是工具调用 ⇒ 非阻断"
    entry = _entries(tmp_path)[-1]
    assert entry["handler"] == "webhook"
    assert entry["command"] == "http://127.0.0.1:8080/hook"
    assert "SSRF check failed" in (entry.get("reason") or "")


async def test_metadata_address_is_blocked(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """云元数据地址同理（169.254.169.254）。"""
    _enable(monkeypatch, allowlist="169.254.169.254")
    assert _register(url="http://169.254.169.254/latest/meta-data/") is True

    assert await hooks.before_tool_use(task=_task(), tool_call=_call()) is None
    assert "SSRF check failed" in (_entries(tmp_path)[-1].get("reason") or "")


# ── ③ 判定语义（假客户端）───────────────────────────────────────────────────


async def test_deny_decision_blocks_tool(
    monkeypatch: pytest.MonkeyPatch, fake_http: type[_FakeClient], tmp_path: Path
) -> None:
    _set_response(200, {"decision": "deny", "reason": "blocked by ops service"})
    _enable(monkeypatch)
    assert _register() is True

    blocked = await hooks.before_tool_use(task=_task(), tool_call=_call())

    assert blocked is not None and "blocked by ops service" in blocked
    last = _entries(tmp_path)[-1]
    assert last["handler"] == "webhook"
    assert last["command"] == FAKE_URL, "URL 摘要要落审计（与 command 形态同一个字段）"
    assert last["blocked"] is True


async def test_two_xx_without_json_allows(
    monkeypatch: pytest.MonkeyPatch, fake_http: type[_FakeClient]
) -> None:
    _set_response(204)
    _enable(monkeypatch)
    assert _register() is True

    assert await hooks.before_tool_use(task=_task(), tool_call=_call()) is None


async def test_non_2xx_is_a_non_blocking_failure(
    monkeypatch: pytest.MonkeyPatch, fake_http: type[_FakeClient], tmp_path: Path
) -> None:
    _set_response(500, b"boom")
    _enable(monkeypatch)
    assert _register() is True

    assert await hooks.before_tool_use(task=_task(), tool_call=_call()) is None
    entry = _entries(tmp_path)[-1]
    assert entry["outcome"] == "error"
    assert entry["exit_code"] == 500


async def test_redirects_are_not_followed(
    monkeypatch: pytest.MonkeyPatch, fake_http: type[_FakeClient], tmp_path: Path
) -> None:
    """3xx 一律按非 2xx 处理：不跟随重定向（跟随会多一条绕过出口检查的路径）。"""
    _set_response(302, b"")
    _enable(monkeypatch)
    assert _register() is True

    assert await hooks.before_tool_use(task=_task(), tool_call=_call()) is None
    assert _FakeClient.instances[-1].kwargs.get("follow_redirects") is False
    assert _entries(tmp_path)[-1]["outcome"] == "error"


async def test_timeout_fails_open_and_is_audited(
    monkeypatch: pytest.MonkeyPatch, fake_http: type[_FakeClient], tmp_path: Path
) -> None:
    import httpx

    _FakeClient.raise_exc = httpx.TimeoutException("too slow")
    _enable(monkeypatch)
    assert _register() is True

    assert await hooks.before_tool_use(task=_task(), tool_call=_call()) is None
    assert _entries(tmp_path)[-1]["outcome"] == "timeout"


async def test_transport_error_fails_open(
    monkeypatch: pytest.MonkeyPatch, fake_http: type[_FakeClient], tmp_path: Path
) -> None:
    _FakeClient.raise_exc = OSError("connection refused")
    _enable(monkeypatch)
    assert _register() is True

    assert await hooks.before_tool_use(task=_task(), tool_call=_call()) is None
    assert _entries(tmp_path)[-1]["outcome"] == "error"


async def test_oversized_response_is_refused(
    monkeypatch: pytest.MonkeyPatch, fake_http: type[_FakeClient], tmp_path: Path
) -> None:
    _set_response(200, b"x" * (128 << 10))
    _enable(monkeypatch)
    assert _register() is True

    assert await hooks.before_tool_use(task=_task(), tool_call=_call()) is None
    assert "exceeds" in (_entries(tmp_path)[-1].get("reason") or "")


# ── ④ 载荷内容无关 ───────────────────────────────────────────────────────────


def test_payload_is_content_free() -> None:
    """载荷只含事件名 / 工具名 / 成败 / 时间戳 —— 内容与租户标识都不出境。"""
    payload = webhook_payload(
        {
            "event": events.PRE_TOOL_USE,
            "tool_name": "shell_exec",
            "ok": False,
            "tool_arguments": {"cmd": "cat ~/.aws/credentials"},
            "tool_result": {"output": "AKIA..."},
            "tenant_id": "t1",
            "user_id": "u1",
            "session_id": "s1",
        }
    )

    assert set(payload) == {"event", "tool_name", "ok", "ts"}
    assert payload["tool_name"] == "shell_exec"
    assert payload["ok"] is False
    assert "tool_arguments" not in payload and "tool_result" not in payload
    assert "t1" not in json.dumps(payload) and "u1" not in json.dumps(payload)


async def test_request_actually_carries_only_that_payload(
    monkeypatch: pytest.MonkeyPatch, fake_http: type[_FakeClient]
) -> None:
    _set_response(200, b"")
    _enable(monkeypatch)
    assert _register() is True

    await hooks.before_tool_use(task=_task(), tool_call=_call())

    method, url, body = _FakeClient.instances[-1].requests[-1]
    assert (method, url) == ("POST", FAKE_URL)
    assert set(body) == {"event", "tool_name", "ts"}
    assert "t_wh" not in json.dumps(body)


# ── ⑤ 声明面 + 多租户 ────────────────────────────────────────────────────────


def test_tenant_cannot_register_webhook(monkeypatch: pytest.MonkeyPatch) -> None:
    _enable(monkeypatch)

    assert hooks.register(
        events.PRE_TOOL_USE, "u_wh", owner=OWNER_USER, handler=HANDLER_WEBHOOK, url=FAKE_URL
    ) is False


def test_declaration_file_loads_webhook_form(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    document = {
        "hooks": {
            events.PRE_TOOL_USE: [
                {"hooks": [{"type": "webhook", "name": "ops", "url": FAKE_URL}]}
            ]
        }
    }
    target = tmp_path / "hooks.json"
    target.write_text(json.dumps(document), encoding="utf-8")
    _enable(monkeypatch, config_path=str(target))

    result = hooks_config.load_hooks_config()

    assert result.failed is False and result.loaded == 1
    assert [hook.handler for hook in hooks.registry.all()] == [HANDLER_WEBHOOK]


def test_declaration_file_webhook_is_skipped_by_default(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    document = {
        "hooks": {
            events.PRE_TOOL_USE: [
                {"hooks": [{"type": "webhook", "name": "ops", "url": FAKE_URL}]}
            ]
        }
    }
    target = tmp_path / "hooks.json"
    target.write_text(json.dumps(document), encoding="utf-8")
    monkeypatch.setattr(settings, "hooks_config_path", str(target))

    result = hooks_config.load_hooks_config()

    assert result.loaded == 0 and result.skipped == 1
    assert hooks.registry.all() == []


def test_declaration_file_missing_url_is_skipped(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    document = {"hooks": {events.PRE_TOOL_USE: [{"hooks": [{"type": "webhook", "name": "ops"}]}]}}
    target = tmp_path / "hooks.json"
    target.write_text(json.dumps(document), encoding="utf-8")
    _enable(monkeypatch, config_path=str(target))

    result = hooks_config.load_hooks_config()

    assert result.loaded == 0 and result.skipped == 1
    assert "config_skipped" in [entry["outcome"] for entry in _entries(tmp_path)]
