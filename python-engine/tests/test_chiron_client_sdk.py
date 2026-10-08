"""Chiron Python 客户端（`clients/python/chiron_client.py`）的契约测试。

用**假 HTTPConnection**（不是 patch 掉 `_request`）—— 这样请求头、请求体、状态码映射、
SSE 逐行解析都是真代码在跑，只有 socket 被换掉。

两条最容易写错的契约在这里被钉住：
① `/v1/agent/submit` 是"**收下即返回**"（202），事件必须另开 `GET /v1/events`；
② SSE 的 `data` 是**信封**：业务负载在 `data.data` 里（顶层只有 type/session_id/id）。
"""

from __future__ import annotations

import http.client
import json
import pathlib
import sys

import pytest

REPO = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "clients" / "python"))

from chiron_client import (  # noqa: E402
    ChironAuthError,
    ChironClient,
    ChironError,
    parse_sse_frame,
)


class _FakeFP:
    """像 socket 文件对象那样**按行**返回（保留空行 —— 空行才是 SSE 的帧分隔符）。"""

    def __init__(self, blob: bytes) -> None:
        parts = blob.split(b"\n")
        # 末尾那一段为空（blob 以 \n 结尾）→ 丢弃；其余每段补回换行
        if parts and parts[-1] == b"":
            parts.pop()
        self._lines = [p + b"\n" for p in parts]

    def readline(self) -> bytes:
        return self._lines.pop(0) if self._lines else b""


class _FakeResponse:
    def __init__(self, status: int, body: bytes = b"", sse_data: bytes = b"") -> None:
        self.status = status
        self._body = body
        self.fp = _FakeFP(sse_data)

    def read(self) -> bytes:
        return self._body


class _FakeConn:
    """记录每次请求；按预设队列返回响应。"""

    calls: list[dict] = []
    responses: list[_FakeResponse] = []

    def __init__(self, host: str, port: int, timeout: float | None = None) -> None:
        self.host, self.port, self.timeout = host, port, timeout

    def request(self, method: str, path: str, body: bytes | None = None, headers: dict | None = None) -> None:
        type(self).calls.append(
            {"method": method, "path": path, "body": body, "headers": headers or {}, "port": self.port}
        )

    def getresponse(self) -> _FakeResponse:
        return type(self).responses.pop(0)

    def close(self) -> None:
        return None


@pytest.fixture(autouse=True)
def _fake_http(monkeypatch):
    _FakeConn.calls = []
    _FakeConn.responses = []
    monkeypatch.setattr(http.client, "HTTPConnection", _FakeConn)
    monkeypatch.setattr(http.client, "HTTPSConnection", _FakeConn)
    yield
    _FakeConn.calls = []
    _FakeConn.responses = []


def _json_response(status: int, payload: dict) -> _FakeResponse:
    return _FakeResponse(status, json.dumps(payload).encode())


def test_login_posts_credentials_and_stores_token():
    _FakeConn.responses = [_json_response(200, {"success": True, "data": {"token": "tk-1", "user": {"id": "u1"}}})]
    client = ChironClient("http://gw:8080")
    out = client.login("a@b.c", "pw")

    assert out["token"] == "tk-1" and client.token == "tk-1"
    call = _FakeConn.calls[0]
    assert (call["method"], call["path"]) == ("POST", "/v1/auth/login")
    assert json.loads(call["body"]) == {"email": "a@b.c", "password": "pw"}
    assert "Authorization" not in call["headers"], "登录前不应带令牌"


def test_submit_is_fire_and_ack_and_carries_bearer():
    _FakeConn.responses = [_json_response(202, {"data": {"status": "accepted", "session_id": "s1"}})]
    client = ChironClient("http://gw:8080", token="tk-1")
    out = client.submit("s1", "hi", llm_config={"mode": "normal"}, context={"kb": "kb1"})

    assert out["status"] == "accepted"
    call = _FakeConn.calls[0]
    assert call["headers"]["Authorization"] == "Bearer tk-1"
    body = json.loads(call["body"])
    assert body == {"session_id": "s1", "content": "hi", "llm_config": {"mode": "normal"}, "context": {"kb": "kb1"}}


def test_cancel_posts_session_id():
    _FakeConn.responses = [_json_response(200, {"data": {"status": "cancelled"}})]
    client = ChironClient("http://gw:8080", token="tk-1")
    assert client.cancel("s1")["status"] == "cancelled"
    assert json.loads(_FakeConn.calls[0]["body"]) == {"session_id": "s1"}


def test_http_errors_map_to_exceptions_with_server_message():
    _FakeConn.responses = [_json_response(401, {"error": "invalid email or password"})]
    with pytest.raises(ChironAuthError) as err:
        ChironClient("http://gw:8080").login("a@b.c", "bad")
    assert err.value.status == 401 and "invalid email or password" in str(err.value)

    _FakeConn.responses = [_json_response(500, {"error": "boom"})]
    with pytest.raises(ChironError) as err2:
        ChironClient("http://gw:8080", token="tk").submit("s1", "hi")
    assert err2.value.status == 500 and not isinstance(err2.value, ChironAuthError)


def test_stream_events_parses_envelope_and_sends_last_event_id():
    frame = json.dumps(
        {"id": "5-0", "type": "text", "session_id": "s1", "data": {"content": "hi"}}
    ).encode()
    _FakeConn.responses = [
        _FakeResponse(
            200,
            sse_data=(
                b"id: 5-0\n"
                b"data: " + frame + b"\n"
                b"\n"
                b"data: " + json.dumps({"type": "connected", "data": {"id": "c1"}}).encode() + b"\n"
                b"\n"
            ),
        )
    ]
    client = ChironClient("http://gw:8080", token="tk-1")
    events = list(client.stream_events("s1", last_event_id="4-0"))

    assert len(events) == 2
    first = events[0]
    assert first.type == "text" and first.id == "5-0" and first.session_id == "s1"
    assert first.data == {"content": "hi"}, "业务负载在信封的 data.data 里"
    assert events[1].type == "connected"

    call = _FakeConn.calls[0]
    assert call["headers"]["Last-Event-ID"] == "4-0"
    assert call["headers"]["Accept"] == "text/event-stream"
    assert "/v1/events?" in call["path"] and "session_id=s1" in call["path"]


def test_wait_for_stops_at_done():
    def _frame(etype: str) -> bytes:
        return b"data: " + json.dumps({"type": etype, "session_id": "s1", "data": {}}).encode() + b"\n\n"

    _FakeConn.responses = [_FakeResponse(200, sse_data=_frame("text") + _frame("done"))]
    client = ChironClient("http://gw:8080", token="tk-1")
    got = client.wait_for("s1", timeout=1)
    assert [e.type for e in got] == ["text", "done"]


def test_parse_sse_frame_rejects_garbage():
    assert parse_sse_frame([]) is None
    assert parse_sse_frame(["data: not-json"]) is None
    assert parse_sse_frame(["data: [1,2]"]) is None
    assert parse_sse_frame([": heartbeat"]) is None
    ev = parse_sse_frame(['event: custom', 'data: {"type":"x"}'])
    assert ev is not None and ev.type == "x"


def test_invalid_base_url_rejected():
    with pytest.raises(ValueError):
        ChironClient("ftp://gw:21")
