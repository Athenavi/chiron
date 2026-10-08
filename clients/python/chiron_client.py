"""Chiron Python 客户端（零第三方依赖）。

## 它是什么 / 不是什么

**是**：把 Chiron 网关的对外 HTTP/SSE 面**收敛成一处**——登录/注册、提交一次对话、
订阅会话事件流（含断线重连的 `Last-Event-ID`）、取消运行。
**不是**：官方承诺兼容性的 SDK。当前形态是**第一方示例客户端**：接口可能随网关演进，
发布/承诺兼容性需要一个明确的产品决定（见 `docs/development-roadmap.md` §2「对外 SDK」）。

## 契约（来自网关实现，不是猜的）

```
POST /v1/auth/login        {email, password, captcha_token?, captcha_randstr?} -> {token, user}
POST /v1/agent/submit      {session_id, content, llm_config?, context?}        -> 202 {status:"accepted"}
POST /v1/agent/cancel      {session_id}                                        -> 200
GET  /v1/events?session_id=&client_id=  (Accept: text/event-stream)            -> SSE 帧
     ↑ 断线重连：请求头 `Last-Event-ID`（或查询参数 last_event_id）
```

两个容易踩的点（都是实测得来的）：

1. **提交是"收下即返回"**：`/v1/agent/submit` 返回 202 后由网关异步跑，**事件不在这条响应里** ——
   必须另外订阅 `GET /v1/events`。把 submit 当 SSE 流来读会一直等不到东西。
2. **SSE 的 `data` 是信封**：`{"id","type","data":{...业务负载...},"session_id"}` ——
   业务字段在 `data.data` 里，不在顶层。
"""

from __future__ import annotations

import http.client
import json
import socket
import urllib.parse
import uuid
from collections.abc import Iterator
from dataclasses import dataclass, field
from typing import Any


class ChironError(Exception):
    """网关返回非 2xx，或响应形状不可解析。"""

    def __init__(self, message: str, *, status: int | None = None, body: str = "") -> None:
        super().__init__(message)
        self.status = status
        self.body = body


class ChironAuthError(ChironError):
    """401/403：令牌缺失、过期或无权访问该会话。"""


@dataclass
class ChironEvent:
    """一条 SSE 事件（信封已拆开：`data` 是业务负载）。"""

    type: str
    data: dict[str, Any] = field(default_factory=dict)
    session_id: str = ""
    id: str = ""
    raw: dict[str, Any] = field(default_factory=dict)


def parse_sse_frame(lines: list[str]) -> ChironEvent | None:
    """把一帧 SSE 行解析成事件；`connected` 之类的心跳帧返回 None（调用方按需保留）。"""
    fields: dict[str, str] = {}
    for line in lines:
        if not line or line.startswith(":"):
            continue
        name, _, value = line.partition(":")
        fields[name.strip()] = value.lstrip()
    raw_text = fields.get("data", "")
    if not raw_text:
        return None
    try:
        envelope = json.loads(raw_text)
    except ValueError:
        return None
    if not isinstance(envelope, dict):
        return None
    inner = envelope.get("data")
    payload = inner if isinstance(inner, dict) else {}
    return ChironEvent(
        type=str(envelope.get("type") or fields.get("event") or ""),
        data=payload,
        session_id=str(envelope.get("session_id") or ""),
        id=str(fields.get("id") or envelope.get("id") or ""),
        raw=envelope,
    )


class ChironClient:
    """最小可用的 Chiron 客户端（零第三方依赖）。"""

    def __init__(
        self,
        base_url: str = "http://127.0.0.1:8080",
        *,
        token: str | None = None,
        timeout: float = 30.0,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.token = token or ""
        self.timeout = float(timeout)
        parsed = urllib.parse.urlparse(self.base_url)
        if parsed.scheme not in ("http", "https") or not parsed.hostname:
            raise ValueError(f"invalid base_url: {base_url!r}")
        self._host = parsed.hostname
        self._port = parsed.port or (443 if parsed.scheme == "https" else 80)
        self._tls = parsed.scheme == "https"
        self._prefix = parsed.path.rstrip("/")

    # ── 内部：HTTP ──

    def _connect(self, timeout: float) -> http.client.HTTPConnection:
        if self._tls:
            return http.client.HTTPSConnection(self._host, self._port, timeout=timeout)
        return http.client.HTTPConnection(self._host, self._port, timeout=timeout)

    def _request(
        self,
        method: str,
        path: str,
        *,
        payload: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
        timeout: float | None = None,
    ) -> tuple[int, dict[str, Any]]:
        """发一次请求并解析 `{success, data}` 信封；非 2xx 抛 `ChironError`。"""
        body = json.dumps(payload).encode() if payload is not None else None
        request_headers = {"Accept": "application/json"}
        if body is not None:
            request_headers["Content-Type"] = "application/json"
        if self.token:
            request_headers["Authorization"] = f"Bearer {self.token}"
        request_headers.update(headers or {})

        conn = self._connect(timeout if timeout is not None else self.timeout)
        try:
            conn.request(method, self._prefix + path, body=body, headers=request_headers)
            resp = conn.getresponse()
            text = resp.read().decode("utf-8", "replace")
            status = resp.status
        finally:
            conn.close()

        if status < 200 or status >= 300:
            message = text[:300]
            try:
                decoded = json.loads(text)
                message = str(decoded.get("error") or decoded.get("message") or message)
            except ValueError:
                pass
            error_cls = ChironAuthError if status in (401, 403) else ChironError
            raise error_cls(f"HTTP {status}: {message}", status=status, body=text)
        try:
            decoded = json.loads(text) if text else {}
        except ValueError as exc:
            raise ChironError(f"invalid JSON response: {exc}", status=status, body=text) from exc
        data = decoded.get("data") if isinstance(decoded, dict) else None
        return status, data if isinstance(data, dict) else (decoded if isinstance(decoded, dict) else {})

    # ── 认证 ──

    def login(self, email: str, password: str, **extra: str) -> dict[str, Any]:
        """登录并保存令牌。返回 `{token, user}`。"""
        _, data = self._request("POST", "/v1/auth/login", payload={"email": email, "password": password, **extra})
        token = str(data.get("token") or "")
        if not token:
            raise ChironError("login response has no token")
        self.token = token
        return data

    def register(self, email: str, password: str, name: str = "", **extra: str) -> dict[str, Any]:
        """注册（若实例开了邮箱验证，需要在 extra 里带 `email_code`）。"""
        _, data = self._request(
            "POST", "/v1/auth/register", payload={"email": email, "password": password, "name": name, **extra}
        )
        token = str(data.get("token") or "")
        if token:
            self.token = token
        return data

    # ── 对话 ──

    def submit(
        self,
        session_id: str,
        content: str,
        *,
        llm_config: dict[str, Any] | None = None,
        context: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """提交一次对话：**收下即返回**（202），事件要从 `stream_events` 拿。"""
        payload: dict[str, Any] = {"session_id": session_id, "content": content}
        if llm_config:
            payload["llm_config"] = llm_config
        if context:
            payload["context"] = context
        _, data = self._request("POST", "/v1/agent/submit", payload=payload)
        return data or {"status": "accepted", "session_id": session_id}

    def cancel(self, session_id: str) -> dict[str, Any]:
        """取消该会话正在跑的运行。"""
        _, data = self._request("POST", "/v1/agent/cancel", payload={"session_id": session_id})
        return data

    # ── 事件流（SSE）──

    def stream_events(
        self,
        session_id: str,
        *,
        last_event_id: str | None = None,
        client_id: str | None = None,
        timeout: float | None = None,
    ) -> Iterator[ChironEvent]:
        """订阅会话事件流（生成器）。

        `last_event_id` 用于断线重连：服务端会先补发该 ID 之后的缺口，再转实时
        （每帧的 `id` 就是可回传的流 ID）。
        """
        query = urllib.parse.urlencode(
            {"session_id": session_id, "client_id": client_id or f"sdk-{uuid.uuid4().hex[:8]}"}
        )
        headers = {"Accept": "text/event-stream"}
        if last_event_id:
            headers["Last-Event-ID"] = last_event_id
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"

        conn = self._connect(timeout if timeout is not None else max(self.timeout, 60.0))
        conn.request("GET", f"{self._prefix}/v1/events?{query}", headers=headers)
        resp = conn.getresponse()
        if resp.status != 200:
            text = resp.read().decode("utf-8", "replace")
            conn.close()
            raise ChironError(f"HTTP {resp.status}: {text[:200]}", status=resp.status, body=text)

        frame: list[str] = []
        try:
            while True:
                try:
                    raw = resp.fp.readline()  # type: ignore[union-attr]
                except (TimeoutError, socket.timeout):
                    return
                if not raw:
                    return
                line = raw.decode("utf-8", "replace").rstrip("\r\n")
                if line == "":
                    event = parse_sse_frame(frame)
                    frame = []
                    if event is not None:
                        yield event
                    continue
                frame.append(line)
        finally:
            conn.close()

    def wait_for(
        self,
        session_id: str,
        *,
        types: tuple[str, ...] = ("done",),
        last_event_id: str | None = None,
        timeout: float = 120.0,
        max_events: int = 10_000,
    ) -> list[ChironEvent]:
        """收事件直到出现 `types` 里的类型（默认 `done`）或超时；返回收到的事件列表。"""
        import time

        collected: list[ChironEvent] = []
        deadline = time.monotonic() + timeout
        for event in self.stream_events(
            session_id, last_event_id=last_event_id, timeout=max(1.0, timeout)
        ):
            collected.append(event)
            if event.type in types:
                return collected
            if len(collected) >= max_events or time.monotonic() > deadline:
                return collected
        return collected
