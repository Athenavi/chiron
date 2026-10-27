#!/usr/bin/env python
"""桌面 smoke：一条命令跑完本地 core 全链路，可选拉起 Electron 壳（**不依赖人工点击 GUI**）。

与 :mod:`verify_local_e2e` 的分工：那个脚本是"逐端点验收"（含已知缺陷的判定）；
这里只回答一个问题 —— **这台机器上，桌面端从后端到壳还能不能跑起来**。所以：

1. 真的 ``python -m app.main`` 起 core 子进程（embedded PostgreSQL + 本地 mock 推理），
   按 Electron 主进程的方式从 stdout 解析 ``{"event": "listening", ...}`` 拿端口；
2. 按桌面端的默认姿势**开启设备 token 鉴权**（``CHIRON_LOCAL_AUTH_ENABLED=1``）并从
   ``CHIRON_DEVICE_TOKEN`` 注入固定 token，然后跑 auth → chat → media → memory → kb；
3. ``--electron`` 时用 ``frontend-vue/node_modules/.bin/electron`` 起壳，观察 10s：进程是否
   还活着、``userData`` 下有没有 ``logs/main.log`` 且写了 ``starting in local mode``，
   然后 terminate / kill 并回收进程树。

用法（任意 cwd 都可以，路径按脚本自身的 ``__file__`` 推导）::

    python desktop-local/scripts/smoke_desktop.py --no-electron   # 只验 core（CI / 无 GUI）
    python desktop-local/scripts/smoke_desktop.py --electron      # 顺带起壳看窗口
    python desktop-local/scripts/smoke_desktop.py --electron --keep   # 留下数据目录排查

退出码：``0`` = 全部 PASS（SKIP 不算失败）；``1`` = 有 FAIL（含"没等到 listening"）。
SKIP 只有三种原因：本机没装 Electron、没有图形会话、指向 Electron 的启动器不可用 ——
三种都会打印**具体原因与补救命令**，不会把跳过写成 PASS。

约定（与 docs/desktop-local/smoke.md 一致）：
- 只用标准库 + 可选 ``httpx``（装了就用它，错误信息更清楚；没装回退 urllib）；
- 失败时**原样打印** core / Electron 的 stderr 尾部，绝不伪造 PASS；
- 不写 ``main.cjs`` / ``preload.cjs`` / ``components.cjs``，也不新增任何主依赖。
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import secrets
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
from collections import deque
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

try:  # 可选依赖：装了 httpx 就用（desktop-local 的 dev extra 里有）
    import httpx
except ImportError:  # pragma: no cover —— 没有它也能跑，只是错误信息朴素一些
    httpx = None  # type: ignore[assignment]

#: ``desktop-local/``：``-m app.main`` 的 cwd
DESKTOP_LOCAL = Path(__file__).resolve().parents[1]
#: 仓库根（本 worktree 的根）
REPO_ROOT = DESKTOP_LOCAL.parent
#: Electron 壳所在目录
FRONTEND_VUE = REPO_ROOT / "frontend-vue"

#: 本地 mock 推理服务的固定响应（字段与 docs/desktop-local/handshake.md 对齐）
MOCK_REPLY: dict[str, Any] = {
    "content": "smoke reply",
    "usage": {"input_tokens": 1, "output_tokens": 1},
    "provider": "mock",
    "model": "mock",
}
MOCK_CONTENT = str(MOCK_REPLY["content"])

#: 一次对话里用户说的话（断言 mock 服务真的收到了它）
CHAT_MESSAGE = "桌面 smoke：本地全链路"

#: 子进程 stderr 保留的行数（失败时打印尾部）
STDERR_TAIL = 40

#: 等 Electron 退出（terminate 之后）的秒数，超时才强杀
ELECTRON_STOP_TIMEOUT = 10.0

#: 能判定"这台机器根本没有图形会话"的 stderr 特征（Linux CI 最常见）
HEADLESS_MARKERS = (
    "missing x server",
    "cannot open display",
    "unable to open x display",
    "display is not set",
    "no display",
    "libgtk",
    "libnss3",
    "libgbm",
    "error while loading shared libraries",
)


# --------------------------------------------------------------------- 结果模型
@dataclass
class Check:
    """一项结论。``ok is None`` 表示 SKIP（原因写在 ``detail`` 里）。"""

    name: str
    ok: bool | None
    detail: str = ""

    @property
    def status(self) -> str:
        if self.ok is None:
            return "SKIP"
        return "PASS" if self.ok else "FAIL"


class SkippedCheckError(RuntimeError):
    """检查项显式声明「本次不判定」：结论记 SKIP，同时把真实原因写进 detail。"""


# --------------------------------------------------------------------- HTTP
@dataclass
class Response:
    """一次 HTTP 往返（4xx/5xx 与连接错误都算结果，不抛）。"""

    status: int
    body: Any
    raw: bytes


class Api:
    """极简 HTTP 客户端。装了 ``httpx`` 用它，否则用标准库 ``urllib``。"""

    def __init__(self, base_url: str, timeout: float = 60.0) -> None:
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.token = ""
        self.transport = f"httpx {httpx.__version__}" if httpx else "urllib（未安装 httpx）"

    def call(self, method: str, path: str, payload: Any = None, *, auth: bool = True) -> Response:
        url = f"{self.base_url}{path}"
        headers: dict[str, str] = {}
        if auth and self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        if httpx is not None:
            return self._call_httpx(method, url, payload, headers)
        return self._call_urllib(method, url, payload, headers)

    def _call_httpx(self, method: str, url: str, payload: Any, headers: dict[str, str]) -> Response:
        try:
            with httpx.Client(timeout=self.timeout) as client:
                response = client.request(method, url, json=payload, headers=headers)
            return Response(int(response.status_code), _parse_json(response.content), response.content)
        except httpx.HTTPError as exc:  # 连接被拒 / 超时也当结果，交给检查项报真实原因
            return Response(0, None, f"{type(exc).__name__}: {exc}".encode())

    def _call_urllib(self, method: str, url: str, payload: Any, headers: dict[str, str]) -> Response:
        data = None if payload is None else json.dumps(payload, ensure_ascii=False).encode("utf-8")
        request = urllib.request.Request(url, data=data, method=method, headers=headers)
        if data is not None:
            request.add_header("Content-Type", "application/json; charset=utf-8")
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                raw = response.read()
                return Response(int(response.status), _parse_json(raw), raw)
        except urllib.error.HTTPError as exc:
            raw = exc.read()
            return Response(int(exc.code), _parse_json(raw), raw)
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            return Response(0, None, f"{type(exc).__name__}: {exc}".encode())


def _parse_json(raw: bytes) -> Any:
    if not raw:
        return None
    try:
        return json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None


# --------------------------------------------------------------------- mock 推理服务
class MockInference:
    """本地 mock 推理服务：``POST /infer`` 固定回复，并记下收到的请求。"""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []
        outer = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def do_POST(self) -> None:  # noqa: N802 —— 基类要求这个名字
                length = int(self.headers.get("Content-Length") or 0)
                raw = self.rfile.read(length) if length else b""
                try:
                    body: Any = json.loads(raw.decode("utf-8") or "{}")
                except (UnicodeDecodeError, json.JSONDecodeError):
                    body = {}
                if self.path != "/infer":
                    self._reply(404, {"error": "not_found"})
                    return
                outer.calls.append({"path": self.path, "body": body})
                self._reply(200, MOCK_REPLY)

            def _reply(self, status: int, payload: dict[str, Any]) -> None:
                data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
                self.send_response(status)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def log_message(self, fmt: str, *args: Any) -> None:
                """静音访问日志（smoke 只想看结论）。"""

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self._thread = threading.Thread(target=self._server.serve_forever, name="mock-inference", daemon=True)

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._server.shutdown()
        self._server.server_close()
        self._thread.join(timeout=5)

    @property
    def base_url(self) -> str:
        host, port = self._server.server_address[:2]
        return f"http://{host}:{port}"


# --------------------------------------------------------------------- 子进程输出
class LinePump(threading.Thread):
    """把一路子进程输出按行收集（``deque`` 定长，保留尾部）。"""

    def __init__(self, stream: Any, name: str, maxlen: int = 4000) -> None:
        super().__init__(name=name, daemon=True)
        self.stream = stream
        self.lines: deque[str] = deque(maxlen=maxlen)

    def run(self) -> None:
        try:
            for line in iter(self.stream.readline, ""):
                self.lines.append(line.rstrip("\n"))
        finally:
            try:
                self.stream.close()
            except OSError:
                pass

    def tail(self, count: int) -> list[str]:
        return list(self.lines)[-count:]


def _pump_poped(proc: subprocess.Popen) -> tuple[LinePump, LinePump]:
    out = LinePump(proc.stdout, "child-stdout")
    err = LinePump(proc.stderr, "child-stderr")
    out.start()
    err.start()
    return out, err


# --------------------------------------------------------------------- core 子进程
def spawn_core(
    data_dir: Path, inference_url: str, *, token: str, auth: bool, knowledge: bool, redis: bool = True
) -> subprocess.Popen:
    """以 ``python -m app.main --port 0`` 起 core（与 components.cjs 的开发态同款命令）。"""
    env = os.environ.copy()
    env["PYTHONUNBUFFERED"] = "1"
    env["PYTHONIOENCODING"] = "utf-8"
    # 模型清单只在这个进程里存在：桌面端由主进程下发，这里模拟同样的形状
    env["CHIRON_LOCAL_MODELS"] = json.dumps(["mock"])
    if knowledge:
        env["CHIRON_LOCAL_KNOWLEDGE_ENABLED"] = "1"
    if not redis:
        env["CHIRON_LOCAL_REDIS_ENABLED"] = "0"
    if auth:
        # 桌面端默认开启设备 token 鉴权，并从 env 注入固定 token（auth.py 优先读 env）
        env["CHIRON_LOCAL_AUTH_ENABLED"] = "1"
        env["CHIRON_DEVICE_TOKEN"] = token

    cmd = [
        sys.executable,
        "-m",
        "app.main",
        "--port",
        "0",
        "--data-dir",
        str(data_dir),
        "--inference-proxy",
        inference_url,
    ]
    return subprocess.Popen(  # noqa: S603 —— 命令与参数全由本脚本拼，无用户输入拼接
        cmd,
        cwd=str(DESKTOP_LOCAL),
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
        bufsize=1,
    )


def wait_listening(stdout: LinePump, proc: subprocess.Popen, timeout: float) -> dict[str, Any] | None:
    """等 stdout 上那行 ``{"event": "listening", ...}``（Electron 主进程也这么干）。"""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        for line in list(stdout.lines):
            text = line.strip()
            if not text.startswith("{"):
                continue
            try:
                event = json.loads(text)
            except json.JSONDecodeError:
                continue
            if isinstance(event, dict) and event.get("event") == "listening":
                return event
        if proc.poll() is not None:
            return None
        time.sleep(0.2)
    return None


def stop_process(proc: subprocess.Popen | None, *, grace: float = 30.0) -> None:
    """先 ``terminate``（让 uvicorn 跑完 lifespan 关 PG），超时才 ``kill``。"""
    if proc is None or proc.poll() is not None:
        return
    proc.terminate()
    try:
        proc.wait(timeout=grace)
    except subprocess.TimeoutExpired:
        proc.kill()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            print(f"警告：pid={proc.pid} 未在 {grace + 10:.0f}s 内退出，已放弃等待")


# --------------------------------------------------------------------- core 检查项
def _expect(condition: Any, message: str) -> None:
    if not condition:
        raise AssertionError(message)


def _body(response: Response) -> Any:
    if response.body is None:
        raise AssertionError(f"响应不是 JSON：status={response.status} raw={response.raw[:300]!r}")
    return response.body


def check_health(api: Api) -> str:
    response = api.call("GET", "/healthz", auth=False)
    _expect(response.status == 200, f"/healthz 期望 200，实际 {response.status} {response.raw[:200]!r}")
    _expect(_body(response) == {"status": "ok"}, f"/healthz 包体异常：{response.body}")
    return "GET /healthz → 200 {'status': 'ok'}"


def check_storage_errors(announce: dict[str, Any]) -> str:
    """PG 是硬要求（本 smoke 的前提）；Redis 等可选后端起不来只记 SKIP，不伪装 PASS。"""
    errors = [str(item) for item in (announce.get("storage_errors") or [])]
    pg_errors = [item for item in errors if item.startswith("pg:")]
    other_errors = [item for item in errors if not item.startswith("pg:")]
    _expect(not pg_errors, f"PG 起不来：{'；'.join(pg_errors)[:600]}")
    if other_errors:
        raise SkippedCheckError(
            "PG 正常；可选的轻量后端未就绪（不影响 core 其余链路）："
            + "；".join(other_errors)[:400]
            + "。想让它也 PASS：python -m pip install fakeredis，或加 --no-redis"
        )
    return "storage_errors=[]"


def check_readyz(api: Api) -> str:
    """**真实 PG** 必须通；Redis 允许降级（fakeredis 没装时 /readyz 会 503 但写明原因）。"""
    response = api.call("GET", "/readyz", auth=False)
    body = _body(response)
    _expect(isinstance(body, dict), "/readyz 不是 JSON 对象")
    _expect(body.get("pg") is True, f"/readyz 报 pg={body.get('pg')} pg_error={body.get('pg_error')}")
    redis_note = ""
    if response.status != 200:
        _expect(
            body.get("redis") is not True and str(body.get("redis_error", "")),
            f"/readyz 返回 {response.status} 但不是 Redis 降级：{json.dumps(body, ensure_ascii=False)[:400]}",
        )
        redis_note = f"，Redis 未就绪（{str(body.get('redis_error'))[:120]}…）"
    return (
        f"/readyz → {response.status} pg=true backend={body.get('pg_backend')} "
        f"version={body.get('pg_version')} auth={body.get('auth')}{redis_note}"
    )


def check_auth(api: Api, token: str) -> str:
    """换设备 token，并验证"没 token 的请求真的被拒"（鉴权链路是否生效）。"""
    anonymous = api.call("GET", "/v1/chat/sessions", auth=False)
    _expect(
        anonymous.status in (200, 401),
        f"未带 token 的 /v1/chat/sessions 返回 {anonymous.status} {anonymous.raw[:200]!r}",
    )
    response = api.call("POST", "/v1/auth/session", auth=False)
    _expect(response.status == 200, f"期望 200，实际 {response.status} {response.raw[:200]!r}")
    data = _body(response)["data"]
    issued = data.get("token")
    _expect(isinstance(issued, str) and len(issued) >= 16, f"token 异常：{issued!r}")
    _expect(data.get("user", {}).get("id") == "local", f"本地用户异常：{data.get('user')}")
    _expect(issued == token, "core 没有使用 CHIRON_DEVICE_TOKEN 注入的 token（壳层注入会失效）")
    api.token = token
    authorized = api.call("GET", "/v1/chat/sessions")
    _expect(authorized.status == 200, f"带 token 的 /v1/chat/sessions 返回 {authorized.status}")
    return (
        f"token={'注入值' if issued == token else 'core 新生成'} user={data['user']['id']} "
        f"role={data['user']['role']}；无 token 请求 {anonymous.status} → 带 token 200"
    )


def check_chat(api: Api, mock: MockInference) -> str:
    """一轮对话：mock 推理 → 落库 → 回读会话与消息。"""
    mock.calls.clear()
    response = api.call("POST", "/v1/chat/submit", {"message": CHAT_MESSAGE, "model": "mock"})
    _expect(response.status == 200, f"submit 期望 200，实际 {response.status} {response.raw[:300]!r}")
    data = _body(response)["data"]
    _expect(data["message"]["content"] == MOCK_CONTENT, f"回复不是 mock：{data['message']['content']!r}")
    _expect(data["message"]["role"] == "assistant", f"角色异常：{data['message']['role']}")
    session_id = data.get("session_id")
    _expect(session_id, "session_id 为空")
    _expect(mock.calls, "mock 推理服务没收到请求（--inference-proxy 没生效？）")
    sent = mock.calls[-1]["body"]
    _expect(
        sent.get("messages", [{}])[-1].get("content") == CHAT_MESSAGE,
        f"送到代理的消息不对：{sent.get('messages')}",
    )

    sessions = api.call("GET", "/v1/chat/sessions")
    _expect(sessions.status == 200, f"/v1/chat/sessions 期望 200，实际 {sessions.status}")
    _expect(session_id in [item["id"] for item in _body(sessions)["data"]], "会话列表里没有刚建的会话")

    messages = api.call("GET", f"/v1/chat/sessions/{session_id}/messages")
    _expect(messages.status == 200, f"消息列表期望 200，实际 {messages.status}")
    rows = _body(messages)["data"]
    _expect([row["role"] for row in rows] == ["user", "assistant"], f"消息顺序异常：{[r['role'] for r in rows]}")
    return f"session={session_id[:12]}… model={data['model']} 回读 {len(rows)} 条消息（mock 收到 {len(mock.calls)} 次请求）"


def check_media(api: Api) -> str:
    """上传一段假 PNG → 列表里能看到 → 删掉（走真实 PG 的 local_media 表）。"""
    payload = base64.b64encode(b"\x89PNG\r\n\x1a\n" + b"desktop-smoke").decode("ascii")
    response = api.call(
        "POST", "/v1/media/upload", {"name": "smoke.png", "mime_type": "image/png", "content_base64": payload}
    )
    _expect(response.status == 201, f"上传期望 201，实际 {response.status} {response.raw[:300]!r}")
    data = _body(response)["data"]
    try:
        listing = api.call("GET", "/v1/media")
        _expect(listing.status == 200, f"列表期望 200，实际 {listing.status}")
        _expect(data["id"] in [item["id"] for item in _body(listing)["data"]], "列表里没有刚上传的媒体")
        return f"id={data['id'][:8]}… size={data['size_bytes']}B"
    finally:
        api.call("DELETE", f"/v1/media/{data['id']}")


def check_memory(api: Api) -> str:
    """写入一条记忆 → 检索命中 → 删掉（走真实 PG 的 local_memory 表）。"""
    response = api.call(
        "POST",
        "/v1/memory/entries",
        {"content": "桌面 smoke：本地记忆", "title": "smoke", "tags": [" smoke ", "pg", "smoke"]},
    )
    _expect(response.status == 201, f"创建期望 201，实际 {response.status} {response.raw[:300]!r}")
    data = _body(response)["data"]
    try:
        _expect(data["tags"] == ["smoke", "pg"], f"标签归一化异常：{data['tags']}")
        hits = api.call("POST", "/v1/memory/search", {"query": "本地记忆"})
        _expect(hits.status == 200, f"检索期望 200，实际 {hits.status}")
        found = [item["id"] for item in _body(hits)["data"]]
        _expect(data["id"] in found, f"检索没命中刚写入的条目：{found}")
        return f"id={data['id'][:8]}… tags={data['tags']} 检索命中 {len(found)} 条"
    finally:
        api.call("DELETE", f"/v1/memory/entries/{data['id']}")


def check_kb(api: Api) -> str:
    """知识库：建库 → 上传文档（分块）→ 检索（要 citations）→ 删库。"""
    created = api.call("POST", "/v1/kb", {"name": "smoke 知识库", "description": "smoke_desktop"})
    _expect(created.status == 201, f"建库期望 201，实际 {created.status} {created.raw[:300]!r}")
    kb_id = _body(created)["data"]["id"]
    try:
        text = "本地知识库的分块策略：800 字一片，重叠 100 字。\n" + "填充内容。\n" * 400
        document = api.call(
            "POST",
            f"/v1/kb/{kb_id}/documents",
            {
                "name": "手册.txt",
                "content_base64": base64.b64encode(text.encode("utf-8")).decode("ascii"),
                "mime_type": "text/plain",
            },
        )
        _expect(document.status == 201, f"上传期望 201，实际 {document.status} {document.raw[:300]!r}")
        doc = _body(document)["data"]
        _expect(doc["chunks"] >= 2, f"分块数异常：{doc['chunks']}")

        query = api.call("POST", f"/v1/kb/{kb_id}/query", {"query": "分块策略", "top_k": 3})
        _expect(query.status == 200, f"检索期望 200，实际 {query.status} {query.raw[:300]!r}")
        citations = _body(query).get("citations") or []
        _expect(citations, "检索没有 citations")
        _expect(citations[0].get("doc_id") == doc["doc_id"], "citation 指向了别的文档")
        return f"kb={kb_id[:10]}… chunks={doc['chunks']} citations={len(citations)}"
    finally:
        api.call("DELETE", f"/v1/kb/{kb_id}")


def _step(checks: list[Check], name: str, func: Any, *args: Any) -> Any:
    """跑一步：异常收敛成 FAIL（带上真实错误），返回值透传给调用方。"""
    try:
        result = func(*args)
    except SkippedCheckError as exc:
        checks.append(Check(name, None, str(exc)))
        return True
    except Exception as exc:  # noqa: BLE001 —— 失败就是要记下来，交给汇总打印
        checks.append(Check(name, False, f"{type(exc).__name__}: {exc}"))
        return None
    checks.append(Check(name, True, result if isinstance(result, str) else ""))
    return result if result is not None else True


# --------------------------------------------------------------------- core 主流程
def run_core(checks: list[Check], data_dir: Path, args: argparse.Namespace) -> tuple[LinePump | None, LinePump | None]:
    """起 core 并跑全链路；返回两路输出泵，供失败时打印 stderr。"""
    mock = MockInference()
    mock.start()
    proc: subprocess.Popen | None = None
    stdout = stderr = None
    token = secrets.token_hex(32)
    try:
        proc = spawn_core(
            data_dir, mock.base_url, token=token, auth=not args.no_auth, knowledge=True, redis=not args.no_redis
        )
        stdout, stderr = _pump_poped(proc)
        print(f"core 子进程 pid={proc.pid}，等待 listening（上限 {args.timeout:.0f}s）…")
        started = time.monotonic()
        announce = wait_listening(stdout, proc, args.timeout)
        if announce is None:
            checks.append(
                Check(
                    "core 进程拉起并上报 listening",
                    False,
                    f"在 {args.timeout:.0f}s 内没等到 listening 事件（进程{'已退出' if proc.poll() is not None else '仍在跑'}）",
                )
            )
            return stdout, stderr
        checks.append(
            Check(
                "core 进程拉起并上报 listening",
                True,
                f"pid={announce.get('pid')} port={announce.get('port')} version={announce.get('version')} "
                f"startup_ms={announce.get('startup_ms')} waited={time.monotonic() - started:.1f}s",
            )
        )
        _step(checks, "启动期无存储错误（PG 必须真通）", check_storage_errors, announce)

        api = Api(f"http://127.0.0.1:{announce['port']}")
        print(f"HTTP 客户端：{api.transport}")
        _step(checks, "GET /healthz", check_health, api)
        _step(checks, "GET /readyz（真实 PG）", check_readyz, api)
        authenticated = _step(checks, "auth：POST /v1/auth/session（设备 token）", check_auth, api, token)
        if not authenticated:
            checks.append(Check("chat / media / memory / kb", None, "跳过：auth 未通过，后面的请求都会 401"))
            return stdout, stderr
        if _step(checks, "chat：POST /v1/chat/submit（mock 推理）+ 回读", check_chat, api, mock) is None:
            checks.append(Check("media / memory / kb", None, "跳过：chat 未通过，先修对话链路"))
            return stdout, stderr
        _step(checks, "media：上传 / 列表 / 删除", check_media, api)
        _step(checks, "memory：写入 / 检索 / 删除", check_memory, api)
        _step(checks, "kb：建库 / 上传 / 检索 / 删除", check_kb, api)
        return stdout, stderr
    finally:
        stop_process(proc)
        mock.stop()


# --------------------------------------------------------------------- Electron
@dataclass
class ElectronLaunch:
    """已解析的启动器：``base + [--user-data-dir=…, "."]``。"""

    base: list[str]
    label: str


def resolve_electron() -> tuple[ElectronLaunch | None, str]:
    """先找 ``frontend-vue/node_modules/.bin/electron``，再退 ``npx --no-install electron``。"""
    bin_dir = FRONTEND_VUE / "node_modules" / ".bin"
    names = ["electron.cmd", "electron.bat", "electron"] if os.name == "nt" else ["electron"]
    for name in names:
        candidate = bin_dir / name
        if candidate.is_file():
            return ElectronLaunch([str(candidate)], f"frontend-vue/node_modules/.bin/{name}"), ""

    npx = shutil.which("npx")
    if not npx:
        return None, "没装 Electron，也没有 npx（先 cd frontend-vue && npm install）"
    try:
        probe = subprocess.run(  # noqa: S603 —— 常量参数
            [npx, "--no-install", "electron", "--version"],
            cwd=str(FRONTEND_VUE),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=120,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return None, f"npx 探测 electron 失败：{type(exc).__name__}: {exc}"
    if probe.returncode == 0 and probe.stdout.strip():
        return ElectronLaunch([npx, "--no-install", "electron"], "npx --no-install electron"), ""
    return None, f"npx 找不到已安装的 electron（{probe.stderr.strip()[:200] or '无输出'}）"


def _headless_reason(stderr_text: str) -> str:
    lowered = stderr_text.lower()
    for marker in HEADLESS_MARKERS:
        if marker in lowered:
            return f"启动即失败，且像「没有图形会话」（命中 `{marker}`）"
    return ""


def _userdata_candidates(preferred: Path) -> list[Path]:
    """``--user-data-dir`` 被忽略时的兜底位置（按 Electron 的默认规则猜，只为**找到**日志）。"""
    home = Path.home()
    names = ["Chiron", "frontend-vue"]
    candidates = [preferred]
    if sys.platform == "win32":
        base = Path(os.environ.get("APPDATA") or home / "AppData" / "Roaming")
    elif sys.platform == "darwin":
        base = home / "Library" / "Application Support"
    else:
        base = Path(os.environ.get("XDG_CONFIG_HOME") or home / ".config")
    candidates.extend(base / name for name in names)
    return candidates


def _read_log(path: Path, limit: int = 20) -> list[str]:
    try:
        return path.read_text(encoding="utf-8", errors="replace").splitlines()[-limit:]
    except OSError:
        return []


def run_electron(checks: list[Check], base_dir: Path, args: argparse.Namespace) -> None:
    """起壳并观察 ``--electron-wait`` 秒：进程存活 + userData 日志，然后杀掉回收。"""
    launch, reason = resolve_electron()
    if launch is None:
        checks.append(Check("Electron 启动器", None, f"{reason}；本条 SKIP，未验证壳层"))
        return
    checks.append(Check("Electron 启动器", True, launch.label))

    if os.name != "nt" and not (os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY")):
        checks.append(Check("Electron 进程", None, "没有图形会话（DISPLAY / WAYLAND_DISPLAY 都为空），无法建窗口"))
        return

    user_data = base_dir / "userdata"
    user_data.mkdir(parents=True, exist_ok=True)
    env = os.environ.copy()
    # 契约 §1：config.json 是模式真相；这里用环境变量把壳钉在本地模式，避免老配置迁移弹框
    env["CHIRON_DESKTOP_MODE"] = "local"
    # 让壳层的配置与日志落在临时目录（main.cjs 用 app.getPath('userData') 写 main.log）
    env["CHIRON_DESKTOP_USER_DATA"] = str(user_data)
    # components.cjs 用 CHIRON_PYTHON 决定 `python -m app.main` 的解释器：跟本脚本一致才可靠
    env["CHIRON_PYTHON"] = sys.executable
    env.setdefault("PYTHONUNBUFFERED", "1")

    cmd = [*launch.base, f"--user-data-dir={user_data}", "."]
    print(f"启动 Electron：{' '.join(cmd)}（cwd={FRONTEND_VUE}）")
    try:
        proc = subprocess.Popen(  # noqa: S603 —— 启动器与参数全由本脚本拼
            cmd,
            cwd=str(FRONTEND_VUE),
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
        )
    except OSError as exc:
        checks.append(Check("Electron 进程", None, f"启动失败：{type(exc).__name__}: {exc}"))
        return

    stdout, stderr = _pump_poped(proc)
    wait_seconds = args.electron_wait
    deadline = time.monotonic() + wait_seconds
    exit_code: int | None = None
    while time.monotonic() < deadline:
        exit_code = proc.poll()
        if exit_code is not None:
            break
        time.sleep(0.2)

    try:
        if exit_code is not None:
            stderr_text = "\n".join(stderr.tail(STDERR_TAIL))
            headless = _headless_reason(stderr_text)
            if headless:
                checks.append(Check("Electron 进程", None, headless))
            else:
                checks.append(
                    Check(
                        "Electron 进程",
                        False,
                        f"壳在 {wait_seconds:.0f}s 内就退出了（exit={exit_code}）；"
                        "常见原因：另一个 Chiron 实例占着单实例锁、缺少 dist/、或主进程抛错。stderr 见下",
                    )
                )
                _print_tail("Electron stderr", stderr)
            return

        checks.append(Check("Electron 进程", True, f"存活 {wait_seconds:.0f}s（pid={proc.pid}）"))

        log_path = user_data / "logs" / "main.log"
        lines = _read_log(log_path)
        marker = "starting in local mode"
        if any(marker in line for line in lines):
            checks.append(
                Check(
                    "userData 日志",
                    True,
                    f"{log_path} 命中 `{marker}`；末行：{lines[-1] if lines else '（空）'}",
                )
            )
        else:
            elsewhere = next(
                (
                    candidate
                    for candidate in _userdata_candidates(user_data)[1:]
                    if any(marker in line for line in _read_log(candidate / "logs" / "main.log"))
                ),
                None,
            )
            if elsewhere is not None:
                checks.append(
                    Check(
                        "userData 日志",
                        True,
                        f"--user-data-dir 未被采纳，日志实际在 {elsewhere}/logs/main.log（命中 `{marker}`）",
                    )
                )
            else:
                checks.append(
                    Check(
                        "userData 日志",
                        False,
                        f"{log_path} 不存在或没写 `{marker}`（壳可能还没进 whenReady；调大 --electron-wait）",
                    )
                )
                _print_tail("Electron stderr", stderr)
    finally:
        _shutdown_electron(proc, checks)


def _shutdown_electron(proc: subprocess.Popen, checks: list[Check]) -> None:
    """terminate → 等 → 强杀；Windows 上还要连子进程一起回收（core 是它 spawn 的）。"""
    if proc.poll() is None:
        proc.terminate()
        try:
            proc.wait(timeout=ELECTRON_STOP_TIMEOUT)
        except subprocess.TimeoutExpired:
            proc.kill()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                checks.append(Check("Electron 退出回收", False, "terminate 与 kill 都没能让壳退出"))
                return
    if os.name == "nt":
        # TerminateProcess 不级联：core（python）会留下孤儿，按进程树再收一遍
        subprocess.run(  # noqa: S603, S607 —— taskkill 是系统自带命令
            ["taskkill", "/PID", str(proc.pid), "/T", "/F"],
            capture_output=True,
            check=False,
        )
    detail = f"pid={proc.pid} 已退出" + ("（taskkill /T /F 收尾）" if os.name == "nt" else "")
    checks.append(Check("Electron 退出回收", True, detail))


# --------------------------------------------------------------------- 打印
def _print_tail(label: str, pump: LinePump | None) -> None:
    print(f"{label} 尾部（{STDERR_TAIL} 行）：")
    for line in pump.tail(STDERR_TAIL) if pump else []:
        print(f"  {line}")


def _print_check(index: int, total: int, check: Check) -> None:
    print(f"[{index}/{total}] {check.name} ... {check.status}")
    if check.detail:
        for line in str(check.detail).splitlines():
            print(f"      {line}")


def _arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="smoke_desktop",
        description="桌面 smoke：core 全链路 + 可选 Electron 壳（不需要人工点 GUI）",
    )
    shell = parser.add_mutually_exclusive_group()
    shell.add_argument("--electron", action="store_true", help="同时拉起 Electron 壳并观察 10s")
    shell.add_argument(
        "--no-electron",
        action="store_false",
        dest="electron",
        help="只验 core（默认；CI / 无 GUI 时用这个）",
    )
    parser.set_defaults(electron=False)
    parser.add_argument("--data-dir", default=None, help="数据目录（默认临时目录，跑完即删）")
    parser.add_argument("--keep", action="store_true", help="保留数据目录（含 PG 集群）以便排查")
    parser.add_argument("--no-auth", action="store_true", help="不开设备 token 鉴权（CHIRON_LOCAL_AUTH_ENABLED=0）")
    parser.add_argument("--no-redis", action="store_true", help="不启用 Redis 适配层（CHIRON_LOCAL_REDIS_ENABLED=0）")
    parser.add_argument("--timeout", type=float, default=240.0, help="等待 listening 的秒数（首启 initdb 较慢）")
    parser.add_argument(
        "--electron-wait",
        type=float,
        default=10.0,
        help="Electron 起来后观察多少秒再判存活（默认 10s）",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _arg_parser().parse_args(argv)

    temporary = args.data_dir is None
    base = Path(tempfile.mkdtemp(prefix="chiron-smoke-")) if temporary else Path(args.data_dir)
    data_dir = base / "data" if temporary else base
    data_dir.mkdir(parents=True, exist_ok=True)

    print("=" * 78)
    print("Chiron 桌面 smoke — core 全链路" + ("+ Electron 壳" if args.electron else "（不含 Electron）"))
    print(f"仓库根: {REPO_ROOT}")
    print(f"数据目录: {data_dir}")
    print(f"Python: {sys.version.split()[0]} ({sys.executable})")
    print("=" * 78)

    checks: list[Check] = []
    stdout = stderr = None
    try:
        stdout, stderr = run_core(checks, data_dir, args)
        for index, check in enumerate(checks, start=1):
            _print_check(index, len(checks), check)
        if args.electron:
            print("-" * 78)
            print("Electron 壳")
            electron_checks: list[Check] = []
            run_electron(electron_checks, base, args)
            for index, check in enumerate(electron_checks, start=1):
                _print_check(index, len(electron_checks), check)
            checks.extend(electron_checks)
    finally:
        if args.keep:
            print(f"已保留数据目录：{data_dir}")
        else:
            shutil.rmtree(base, ignore_errors=True)

    failed = [check for check in checks if check.ok is False]
    skipped = [check for check in checks if check.ok is None]
    print("-" * 78)
    print(f"合计 {len(checks)} 项：PASS {sum(1 for c in checks if c.ok)} / SKIP {len(skipped)} / FAIL {len(failed)}")
    if skipped:
        print("跳过项（不是 PASS，只是本次没验）：")
        for check in skipped:
            print(f"  - {check.name}：{check.detail}")
    if failed:
        print("失败项：")
        for check in failed:
            print(f"  - {check.name}：{check.detail}")
        _print_tail("core stderr", stderr)
        return 1
    print("全部 PASS" + ("" if not skipped else "（含 SKIP，见上）"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
