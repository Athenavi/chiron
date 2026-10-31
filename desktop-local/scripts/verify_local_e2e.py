#!/usr/bin/env python
"""本地 core 端到端验证：**真起进程 + 真打 API**（契约 §4 / §6）。

与 ``tests/integration/`` 的分工：pytest 版在进程内用 ``TestClient``；这里真的
``python -m app.main --port 0`` 拉起 core 子进程，按 Electron 主进程的方式从 stdout 解析
``{"event": "listening", ...}`` 那一行，再逐个打 API 并打印 PASS/FAIL。失败时保留子进程
stderr 尾部，方便直接看出是"本地库没起来"还是"端点坏了"。

推理不依赖任何云端服务：脚本自己在本机起一个 mock 推理服务（标准库 ``http.server``），
``POST /infer`` 固定回 ``{"content": "mock reply", "usage": {...}, "provider": "mock"}``，
再用 ``--inference-proxy`` 把 core 指过去。

用法::

    cd desktop-local
    python scripts/verify_local_e2e.py                     # 临时数据目录，跑完即删
    python scripts/verify_local_e2e.py --keep              # 保留数据目录（含 PG）以便排查
    python scripts/verify_local_e2e.py --data-dir D:\\chiron-e2e
    python scripts/verify_local_e2e.py --no-redis          # 不启用 Redis 适配层
    python scripts/verify_local_e2e.py --timeout 300       # 首启 initdb 慢的机器上放宽

退出码：0 = 全部 PASS；1 = 有 FAIL（含本机缺 fakeredis 导致的 ``/readyz`` 降级）。
每项都带 ``[known]`` 标记时说明这是**已知缺陷**（见输出末尾的说明），不是脚本自身出错。
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
from collections import deque
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

#: scripts/ 的上一级就是 desktop-local/（``-m app.main`` 的 cwd）
DESKTOP_LOCAL = Path(__file__).resolve().parents[1]

#: 与契约 §9 一致的 mock 响应（``usage`` 用 input/output 字段，这是 app bug #1 的证据）
MOCK_REPLY: dict[str, Any] = {
    "content": "mock reply",
    "usage": {"input_tokens": 1, "output_tokens": 1},
    "provider": "mock",
    "model": "mock",
}

#: 要断言的固定回复文本
MOCK_CONTENT = str(MOCK_REPLY["content"])

#: 子进程 stderr 保留的行数（失败时打印尾部）
STDERR_TAIL = 40


# --------------------------------------------------------------------- 结果模型
@dataclass
class Check:
    """一项结论。``ok is None`` 表示跳过；``known_issue`` 非空表示"已知缺陷"。"""

    name: str
    ok: bool | None
    detail: str = ""
    known_issue: str = ""

    @property
    def status(self) -> str:
        if self.ok is None:
            return "SKIP"
        return "PASS" if self.ok else "FAIL"


@dataclass
class Response:
    """一次 HTTP 往返的结果（错误状态码也算结果，不抛）。"""

    status: int
    body: Any
    raw: bytes
    headers: dict[str, str] = field(default_factory=dict)


# --------------------------------------------------------------------- HTTP
class Api:
    """极简 HTTP 客户端（只依赖标准库，不给验证脚本加依赖）。"""

    def __init__(self, base_url: str, timeout: float = 60.0) -> None:
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout

    def call(self, method: str, path: str, payload: Any = None) -> Response:
        url = f"{self.base_url}{path}"
        data = None if payload is None else json.dumps(payload, ensure_ascii=False).encode("utf-8")
        request = urllib.request.Request(url, data=data, method=method)
        if data is not None:
            request.add_header("Content-Type", "application/json; charset=utf-8")
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                raw = response.read()
                status = int(response.status)
                headers = {key.lower(): value for key, value in response.headers.items()}
        except urllib.error.HTTPError as exc:  # 4xx / 5xx 也要看包体
            raw = exc.read()
            status = int(exc.code)
            headers = {key.lower(): value for key, value in (exc.headers or {}).items()}
        return Response(status=status, body=_parse_json(raw), raw=raw, headers=headers)


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
                """静音访问日志。"""

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


# --------------------------------------------------------------------- core 子进程
class LinePump(threading.Thread):
    """把子进程的一路输出按行收集（``deque`` 定长，尾部优先保留）。"""

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

    def text(self) -> str:
        return "\n".join(self.lines)

    def tail(self, count: int) -> list[str]:
        return list(self.lines)[-count:]


def spawn_core(
    data_dir: Path, inference_url: str, *, no_redis: bool, knowledge: bool
) -> tuple[subprocess.Popen, LinePump, LinePump]:
    """以 ``python -m app.main --port 0`` 起 core，返回进程与两路输出泵。"""
    env = os.environ.copy()
    env["PYTHONUNBUFFERED"] = "1"
    env["PYTHONIOENCODING"] = "utf-8"
    env["CHIRON_LOCAL_MODELS"] = json.dumps(["mock"])
    if knowledge:
        env["CHIRON_LOCAL_KNOWLEDGE_ENABLED"] = "1"
    if no_redis:
        env["CHIRON_LOCAL_REDIS_ENABLED"] = "0"

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
    proc = subprocess.Popen(  # noqa: S603 —— 命令与参数全由本脚本拼，无用户输入拼接
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
    out = LinePump(proc.stdout, "core-stdout")
    err = LinePump(proc.stderr, "core-stderr")
    out.start()
    err.start()
    return proc, out, err


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


def wait_storage(api: Api, timeout: float) -> tuple[bool, str]:
    """等存储初始化完成（``/readyz`` 的 ``pg`` 变成 true）。

    ``app/main.py`` 把 PG 启动与迁移放在**后台 task** 里（见 ``api/health.py`` 里
    ``storage bootstrap in progress`` 那段），所以 ``listening`` 事件先到、存储后到。
    不在这里等，后面所有业务接口都会拿到 503，把"还在初始化"误报成"端点坏了"。

    Redis 不参与判定：它未就绪只让 ``/readyz`` 降级为 ``degraded``，业务端点对
    redis 零引用（理由见 ``api/health.py`` 的注释）。
    """
    deadline = time.monotonic() + timeout
    started = time.monotonic()
    last = "还没打过 /readyz"
    while time.monotonic() < deadline:
        response = api.call("GET", "/readyz")
        body = response.body if isinstance(response.body, dict) else {}
        if body.get("pg") is True:
            waited = time.monotonic() - started
            return True, (
                f"pg={body.get('pg_backend')} version={body.get('pg_version')} waited={waited:.1f}s"
                f" startup_ms={body.get('startup_ms')}"
            )
        last = f"status={response.status} pg_error={body.get('pg_error')!r}"
        time.sleep(0.5)
    return False, f"{timeout:.0f}s 内存储没就绪：{last}"


def stop_core(proc: subprocess.Popen | None) -> None:
    """先 ``terminate``（让 uvicorn 跑完 lifespan 关 PG），超时才 ``kill``。"""
    if proc is None or proc.poll() is not None:
        return
    proc.terminate()
    try:
        proc.wait(timeout=30)
    except subprocess.TimeoutExpired:
        proc.kill()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            print("警告：core 子进程未在 40s 内退出，已放弃等待")


# --------------------------------------------------------------------- 检查项
def _expect(condition: Any, message: str) -> None:
    if not condition:
        raise AssertionError(message)


def _body(response: Response) -> Any:
    if response.body is None:
        raise AssertionError(f"响应不是 JSON：status={response.status} raw={response.raw[:200]!r}")
    return response.body


def check_health(api: Api) -> str:
    response = api.call("GET", "/healthz")
    _expect(response.status == 200, f"/healthz 期望 200，实际 {response.status}")
    _expect(_body(response) == {"status": "ok"}, f"/healthz 包体异常：{response.body}")
    return "200 {'status': 'ok'}"


def check_readyz(api: Api) -> str:
    """``/readyz`` 要么 200，要么 503 但**明确**说明只有 Redis 缺后端（PG 必须真的通）。"""
    response = api.call("GET", "/readyz")
    body = _body(response)
    _expect(isinstance(body, dict), "/readyz 不是 JSON 对象")
    _expect(body.get("pg") is True, f"/readyz 报 pg={body.get('pg')} error={body.get('pg_error')}")
    if response.status == 200:
        return f"200 pg=true backend={body.get('pg_backend')} version={body.get('pg_version')}"
    _expect(
        body.get("redis") is not True and str(body.get("redis_error", "")),
        f"/readyz 返回 {response.status} 但不是 Redis 降级：{json.dumps(body, ensure_ascii=False)[:400]}",
    )
    return f"{response.status}（PG 正常，Redis 未就绪：{body.get('redis_error')}）"


def check_auth(api: Api) -> str:
    response = api.call("POST", "/v1/auth/session")
    _expect(response.status == 200, f"期望 200，实际 {response.status} {response.raw[:200]!r}")
    data = _body(response)["data"]
    _expect(isinstance(data.get("token"), str) and len(data["token"]) >= 16, f"token 异常：{data.get('token')!r}")
    _expect(data.get("user", {}).get("id") == "local", f"本地用户异常：{data.get('user')}")
    return f"token={data['token'][:6]}… user={data['user']['id']} role={data['user']['role']}"


def check_models(api: Api) -> str:
    response = api.call("GET", "/v1/models")
    _expect(response.status == 200, f"期望 200，实际 {response.status}")
    data = _body(response)["data"]
    _expect(any(item.get("id") == "mock" for item in data), f"模型清单里没有 mock：{data}")
    return f"{len(data)} 个模型：{[item.get('id') for item in data]}"


def check_models_verify(api: Api) -> str:
    response = api.call("POST", "/v1/models/verify", {"model": "mock"})
    _expect(response.status == 200, f"期望 200，实际 {response.status} {response.raw[:200]!r}")
    data = _body(response)["data"]
    _expect(data.get("ok") is True, f"verify 未通过：{data}")
    _expect(data.get("reply") == MOCK_CONTENT, f"verify 回复不是 mock：{data.get('reply')!r}")
    return f"ok=true model={data.get('model')} latency={data.get('latency_ms')}ms"


def check_chat(api: Api, mock: MockInference) -> str:
    """提交一轮对话，并把会话 / 消息（真实落库后）回读一遍。"""
    mock.calls.clear()
    response = api.call("POST", "/v1/chat/submit", {"message": "端到端验证", "model": "mock"})
    _expect(response.status == 200, f"期望 200，实际 {response.status} {response.raw[:300]!r}")
    data = _body(response)["data"]
    _expect(data["message"]["content"] == MOCK_CONTENT, f"回复不是 mock：{data['message']['content']!r}")
    _expect(data["message"]["role"] == "assistant", f"角色异常：{data['message']['role']}")
    session_id = data["session_id"]
    _expect(session_id, "session_id 为空")

    _expect(mock.calls, "mock 推理服务没收到请求")
    sent = mock.calls[-1]["body"]
    _expect(
        sent.get("messages", [{}])[-1].get("content") == "端到端验证", f"送到代理的消息不对：{sent.get('messages')}"
    )
    _expect(sent.get("model") == "mock", f"送到代理的模型不对：{sent.get('model')!r}")

    sessions = api.call("GET", "/v1/chat/sessions")
    _expect(sessions.status == 200, f"/v1/chat/sessions 期望 200，实际 {sessions.status}")
    ids = [item["id"] for item in _body(sessions)["data"]]
    _expect(session_id in ids, f"会话列表里没有刚建的会话：{ids[:5]}")

    messages = api.call("GET", f"/v1/chat/sessions/{session_id}/messages")
    _expect(messages.status == 200, f"消息列表期望 200，实际 {messages.status}")
    rows = _body(messages)["data"]
    roles = [row["role"] for row in rows]
    _expect(roles == ["user", "assistant"], f"消息顺序异常：{roles}")
    _expect(rows[1]["content"] == MOCK_CONTENT, "assistant 消息内容不对")

    return (
        f"session={session_id[:12]}… model={data['model']} usage_estimated={data['usage_estimated']} "
        f"history={data['history_count']}；回读 {len(rows)} 条消息"
    )


def check_agents(api: Api) -> str:
    created = api.call(
        "POST",
        "/v1/agents",
        {"name": "端到端 Agent", "system_prompt": "只回答本地事实。", "model": "mock", "tools": ["search"]},
    )
    _expect(created.status == 201, f"创建期望 201，实际 {created.status} {created.raw[:300]!r}")
    agent = _body(created)["data"]
    agent_id = agent["id"]
    try:
        _expect(agent["tools"] == ["search"], f"tools 往返异常：{agent['tools']}")
        _expect(agent["tenant_id"] == 1 and agent["user_id"] == "local", f"租户字段异常：{agent}")

        fetched = api.call("GET", f"/v1/agents/{agent_id}")
        _expect(fetched.status == 200, f"详情期望 200，实际 {fetched.status}")
        _expect(_body(fetched)["data"]["name"] == "端到端 Agent", "详情名字不对")

        listed = api.call("GET", "/v1/agents")
        _expect(listed.status == 200, f"列表期望 200，实际 {listed.status}")
        _expect(agent_id in [item["id"] for item in _body(listed)["data"]], "列表里没有新建的 Agent")
        return f"id={agent_id[:12]}… tools={agent['tools']}"
    finally:
        removed = api.call("DELETE", f"/v1/agents/{agent_id}")
        _expect(removed.status == 200, f"删除期望 200，实际 {removed.status}")
        _expect(api.call("GET", f"/v1/agents/{agent_id}").status == 404, "删除后详情应为 404")


def check_media_upload(api: Api) -> str:
    """**已知缺陷**（app bug #4）：真实 PG 上 ``insert_media`` 的列数与占位符数不匹配。"""
    import base64

    payload = base64.b64encode(b"\x89PNG\r\n\x1a\n" + b"verify-local-e2e").decode("ascii")
    response = api.call(
        "POST", "/v1/media/upload", {"name": "verify.png", "mime_type": "image/png", "content_base64": payload}
    )
    _expect(response.status == 201, f"上传期望 201，实际 {response.status} {response.raw[:300]!r}")
    data = _body(response)["data"]
    try:
        listing = api.call("GET", "/v1/media")
        _expect(listing.status == 200, f"列表期望 200，实际 {listing.status}")
        _expect(data["id"] in [item["id"] for item in _body(listing)["data"]], "列表里没有刚上传的媒体")
        return f"id={data['id'][:8]}… size={data['size_bytes']}"
    finally:
        api.call("DELETE", f"/v1/media/{data['id']}")


def check_media_list(api: Api) -> str:
    response = api.call("GET", "/v1/media")
    _expect(response.status == 200, f"期望 200，实际 {response.status} {response.raw[:200]!r}")
    data = _body(response)["data"]
    _expect(isinstance(data, list), f"data 不是列表：{type(data).__name__}")
    return f"{len(data)} 条媒体元数据"


def check_memory_create(api: Api) -> str:
    """**已知缺陷**（app bug #4）：真实 PG 上 ``insert_entry`` 的列数与占位符数不匹配。"""
    response = api.call(
        "POST",
        "/v1/memory/entries",
        {"content": "端到端验证：本地记忆", "title": "验证", "tags": [" e2e ", "pg", "e2e"]},
    )
    _expect(response.status == 201, f"创建期望 201，实际 {response.status} {response.raw[:300]!r}")
    data = _body(response)["data"]
    try:
        _expect(data["tags"] == ["e2e", "pg"], f"标签归一化异常：{data['tags']}")
        hits = api.call("POST", "/v1/memory/search", {"query": "本地记忆"})
        _expect(hits.status == 200, f"检索期望 200，实际 {hits.status}")
        _expect(data["id"] in [item["id"] for item in _body(hits)["data"]], "检索没命中刚写入的条目")
        return f"id={data['id'][:8]}… tags={data['tags']}"
    finally:
        api.call("DELETE", f"/v1/memory/entries/{data['id']}")


def check_memory_search(api: Api) -> str:
    response = api.call("POST", "/v1/memory/search", {"query": "端到端"})
    _expect(response.status == 200, f"期望 200，实际 {response.status} {response.raw[:200]!r}")
    data = _body(response)["data"]
    _expect(isinstance(data, list), f"data 不是列表：{type(data).__name__}")
    blank = api.call("POST", "/v1/memory/search", {"query": "   "})
    _expect(blank.status == 400, f"空白 query 期望 400，实际 {blank.status}")
    return f"{len(data)} 条命中；空白 query 正确返回 400"


def check_knowledge(api: Api) -> str:
    """建库 → 上传文档 → 检索（含 citations）→ 删库。"""
    import base64

    created = api.call("POST", "/v1/kb", {"name": "端到端知识库", "description": "verify_local_e2e"})
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
        result = _body(query)
        citations = result.get("citations") or []
        _expect(citations, f"检索没有 citations：{result}")
        _expect(citations[0].get("doc_id") == doc["doc_id"], "citation 指向了别的文档")

        listed = api.call("GET", "/v1/kb")
        _expect(listed.status == 200, f"列库期望 200，实际 {listed.status}")
        _expect(kb_id in [item["id"] for item in _body(listed)["data"]], "列库里没有刚建的库")
        return f"kb={kb_id[:10]}… chunks={doc['chunks']} citations={len(citations)}"
    finally:
        removed = api.call("DELETE", f"/v1/kb/{kb_id}")
        _expect(removed.status == 200, f"删库期望 200，实际 {removed.status}")


def check_conversations(api: Api) -> str:
    """会话地图数据面：``/v1/conversations`` 必须真的列出本地会话。

    这组端点此前**只存在于 Go 网关**，本地模式下 404，地图因此拿不到任何卡片
    （见 ``app/api/conversations.py`` 模块注释里记录的探针结果）。本检查把它钉在 e2e 里，
    顺带确认"本地没有的能力"明确回 501，而不是假装成功。
    """
    listed = api.call("GET", "/v1/conversations")
    _expect(listed.status == 200, f"列表期望 200，实际 {listed.status} {listed.raw[:300]!r}")
    items = _body(listed)["data"]
    _expect(items, "会话列表为空：前面 /v1/chat/submit 建的会话没落库？")

    first = items[0]
    # 地图的 adapter.js 依赖这些字段存在；云端才有的字段本地返回空值，但**必须在**
    for key in ("id", "title", "model", "message_count", "tag", "alias", "parent_title", "branch_from_seq"):
        _expect(key in first, f"会话缺少字段 {key}：{sorted(first)}")

    detail = api.call("GET", f"/v1/conversations/{first['id']}")
    _expect(detail.status == 200, f"详情期望 200，实际 {detail.status} {detail.raw[:300]!r}")
    payload = _body(detail)["data"]
    _expect(isinstance(payload.get("messages"), list), f"详情里的 messages 不是数组：{payload.get('messages')!r}")

    missing = api.call("GET", "/v1/conversations/no-such-conversation")
    _expect(missing.status == 404, f"不存在的会话期望 404，实际 {missing.status}")

    # 删除 / 分叉在本地由宿主代理执行；端点必须明确拒绝，不能回 200 让前端以为成了
    for method, path, body in (
        ("DELETE", f"/v1/conversations/{first['id']}", None),
        ("POST", f"/v1/conversations/{first['id']}/fork", {}),
    ):
        response = api.call(method, path, body)
        _expect(response.status == 501, f"{method} {path} 期望 501，实际 {response.status}")

    return f"{len(items)} 个会话；详情含 {len(payload['messages'])} 条消息；删除/分叉明确 501"


def check_session_map(api: Api) -> str:
    """会话地图布局面：建画布 → 存节点 → 回读 → 分叉节点 → 删画布。

    ``PUT`` 是**整体替换**（地图拖动 800ms 防抖后一次性提交），所以这里重点验证
    "写进去的节点数 / viewport 能原样读回来"，而不是逐个字段更新。
    """
    created = api.call("POST", "/v1/session-map/workspaces", {"name": "端到端画布"})
    _expect(created.status == 200, f"建画布期望 200，实际 {created.status} {created.raw[:300]!r}")
    workspace = _body(created)["data"]
    workspace_id = workspace["id"]
    _expect(workspace["name"] == "端到端画布", f"画布名不对：{workspace.get('name')!r}")
    _expect(workspace["nodes"] == [], f"新画布不该有节点：{workspace['nodes']}")

    try:
        nodes = [
            {"id": "n1", "title": "第一个", "x": 10, "y": 20},
            {"id": "n2", "title": "第二个", "x": 30, "y": 40, "parent_node_id": "n1"},
        ]
        saved = api.call(
            "PUT",
            f"/v1/session-map/workspaces/{workspace_id}",
            {"name": "改名后的画布", "viewport": {"x": 1, "y": 2, "zoom": 0.5}, "nodes": nodes},
        )
        _expect(saved.status == 200, f"保存期望 200，实际 {saved.status} {saved.raw[:300]!r}")
        body = _body(saved)["data"]
        _expect(body["name"] == "改名后的画布", f"改名没生效：{body['name']!r}")
        _expect(body["viewport"] == {"x": 1, "y": 2, "zoom": 0.5}, f"viewport 没落库：{body['viewport']!r}")
        _expect(len(body["nodes"]) == 2, f"节点数不对：{len(body['nodes'])}")

        # 回读：地图刷新后要能恢复布局，所以必须真的从库里读
        fetched = api.call("GET", f"/v1/session-map/workspaces/{workspace_id}")
        _expect(fetched.status == 200, f"回读期望 200，实际 {fetched.status}")
        again = _body(fetched)["data"]
        _expect({n["id"] for n in again["nodes"]} == {"n1", "n2"}, f"回读节点不对：{again['nodes']}")

        listed = api.call("GET", "/v1/session-map/workspaces")
        _expect(listed.status == 200, f"列画布期望 200，实际 {listed.status}")
        ids = [item["id"] for item in _body(listed)["data"]["workspaces"]]
        _expect(workspace_id in ids, f"画布列表里没有刚建的：{ids[:5]}")

        forked = api.call(
            "POST",
            f"/v1/session-map/workspaces/{workspace_id}/fork-node",
            {"parent_node_id": "n1", "x": 50, "y": 60, "title": "分叉节点"},
        )
        _expect(forked.status == 200, f"分叉节点期望 200，实际 {forked.status} {forked.raw[:300]!r}")
        node = _body(forked)["data"]
        _expect(node["edge_kind"] == "branch", f"分叉节点的 edge_kind 不对：{node['edge_kind']!r}")
        _expect(node["parent_node_id"] == "n1", f"分叉节点父指针不对：{node['parent_node_id']!r}")
        detail = f"ws={workspace_id[:10]}… nodes={len(again['nodes'])}+1 viewport 已落库"
    finally:
        removed = api.call("DELETE", f"/v1/session-map/workspaces/{workspace_id}")
        _expect(removed.status == 200, f"删画布期望 200，实际 {removed.status}")

    # 删完再读必须 404：确认删除不是"只是不显示"
    gone = api.call("GET", f"/v1/session-map/workspaces/{workspace_id}")
    _expect(gone.status == 404, f"删除后回读期望 404，实际 {gone.status}")
    return detail


# --------------------------------------------------------------------- 主流程
def run(checks: list[Check], name: str, func: Any, *args: Any, known_issue: str = "") -> Any:
    """跑一项检查：异常收敛成 FAIL（不中断后续检查），返回值透传给调用方。"""
    try:
        result = func(*args)
    except Exception as exc:  # noqa: BLE001 —— 失败就是要记下来，交给汇总打印
        checks.append(Check(name, False, f"{type(exc).__name__}: {exc}", known_issue))
        return None
    detail = result if isinstance(result, str) else ""
    checks.append(Check(name, True, detail, known_issue))
    return result


def _print_check(index: int, total: int, check: Check) -> None:
    suffix = f"  [known] {check.known_issue}" if check.known_issue and check.ok is False else ""
    print(f"[{index}/{total}] {check.name} ... {check.status}{suffix}")
    if check.detail:
        for line in str(check.detail).splitlines():
            print(f"      {line}")


def _arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="verify_local_e2e", description="本地 core 端到端验证（真起进程 + 真打 API）")
    parser.add_argument("--data-dir", default=None, help="数据目录（默认临时目录，跑完即删）")
    parser.add_argument("--keep", action="store_true", help="保留数据目录（含 PG 集群）以便排查")
    parser.add_argument("--no-redis", action="store_true", help="不启用 Redis 适配层（CHIRON_LOCAL_REDIS_ENABLED=0）")
    parser.add_argument("--no-knowledge", action="store_true", help="不打开知识库开关（跳过 /v1/kb 检查）")
    parser.add_argument("--timeout", type=float, default=240.0, help="等待 listening 的秒数（首启 initdb 较慢）")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _arg_parser().parse_args(argv)

    temporary = args.data_dir is None
    base = Path(tempfile.mkdtemp(prefix="chiron-e2e-")) if temporary else Path(args.data_dir)
    data_dir = base / "data" if temporary else base
    data_dir.mkdir(parents=True, exist_ok=True)

    print("=" * 78)
    print("Chiron 本地 core — 端到端验证（docs/desktop-local/contracts.md §4）")
    print(f"数据目录: {data_dir}")
    print("=" * 78)

    mock = MockInference()
    mock.start()
    proc: subprocess.Popen | None = None
    stdout: LinePump | None = None
    stderr: LinePump | None = None
    checks: list[Check] = []

    try:
        proc, stdout, stderr = spawn_core(
            data_dir,
            mock.base_url,
            no_redis=args.no_redis,
            knowledge=not args.no_knowledge,
        )
        print(f"core 子进程 pid={proc.pid}，等待 listening（上限 {args.timeout:.0f}s）…")
        started = time.monotonic()
        announce = wait_listening(stdout, proc, args.timeout)
        if announce is None:
            print("FAIL 没等到 listening 事件（进程可能已退出）")
            print(f"core stderr 尾部（{STDERR_TAIL} 行）：")
            for line in stderr.tail(STDERR_TAIL) if stderr else []:
                print(f"  {line}")
            return 1

        elapsed = time.monotonic() - started
        checks.append(
            Check(
                "进程拉起并上报 listening",
                True,
                f"pid={announce.get('pid')} port={announce.get('port')} version={announce.get('version')} "
                f"startup_ms={announce.get('startup_ms')} waited={elapsed:.1f}s",
            )
        )
        storage_errors = announce.get("storage_errors") or []
        checks.append(
            Check(
                "启动期无存储错误",
                not storage_errors,
                "、".join(str(item) for item in storage_errors) if storage_errors else "storage_errors=[]",
            )
        )

        api = Api(f"http://127.0.0.1:{announce['port']}")
        # listening 只说明 HTTP 端口开了；PG 启动与迁移在后台 task 里跑，必须等它就绪，
        # 否则下面所有业务接口都会拿到 503，把"还在初始化"误报成"端点坏了"。
        storage_ok, storage_detail = wait_storage(api, args.timeout)
        checks.append(Check("存储初始化完成（/readyz pg=true）", storage_ok, storage_detail))
        if not storage_ok:
            print("FAIL 存储没就绪，后续业务接口必然 503，先看 core stderr：")
            for line in stderr.tail(STDERR_TAIL) if stderr else []:
                print(f"  {line}")
            for index, check in enumerate(checks, start=1):
                _print_check(index, len(checks), check)
            return 1

        run(checks, "GET /healthz", check_health, api)
        run(checks, "GET /readyz", check_readyz, api)
        run(checks, "POST /v1/auth/session", check_auth, api)
        run(checks, "GET /v1/models", check_models, api)
        run(checks, "POST /v1/models/verify（mock 推理）", check_models_verify, api)
        run(checks, "POST /v1/chat/submit + 会话/消息回读（mock 推理）", check_chat, api, mock)
        # 会话地图的数据面与布局面：此前只存在于 Go 网关，本地 404 过，必须钉住
        run(checks, "GET /v1/conversations 列表/详情/501 兜底", check_conversations, api)
        run(checks, "session-map 建/存/回读/分叉/删画布", check_session_map, api)
        run(checks, "POST/GET/DELETE /v1/agents", check_agents, api)
        run(
            checks,
            "POST /v1/media/upload + 列表",
            check_media_upload,
            api,
            known_issue="app bug #4：files.insert_media 列数(7)与占位符(6)不匹配",
        )
        run(checks, "GET /v1/media", check_media_list, api)
        run(
            checks,
            "POST /v1/memory/entries + 检索",
            check_memory_create,
            api,
            known_issue="app bug #4：memory_store.insert_entry 列数(7)与占位符(6)不匹配",
        )
        run(checks, "POST /v1/memory/search", check_memory_search, api)
        if args.no_knowledge:
            checks.append(Check("POST /v1/kb 全流程", None, "跳过（--no-knowledge）"))
        else:
            run(checks, "POST /v1/kb 建库/上传/检索/删库", check_knowledge, api)

        for index, check in enumerate(checks, start=1):
            _print_check(index, len(checks), check)
    finally:
        stop_core(proc)
        mock.stop()
        if not args.keep:
            shutil.rmtree(base, ignore_errors=True)
        else:
            print(f"已保留数据目录：{data_dir}")

    failed = [check for check in checks if check.ok is False]
    known = [check for check in failed if check.known_issue]
    print("-" * 78)
    print(
        f"合计 {len(checks)} 项：PASS {sum(1 for c in checks if c.ok)} / SKIP {sum(1 for c in checks if c.ok is None)} / FAIL {len(failed)}"
    )
    if failed:
        print("失败项：")
        for check in failed:
            tag = f"（已知缺陷：{check.known_issue}）" if check.known_issue else ""
            print(f"  - {check.name}{tag}")
        if known:
            print("注：标 [known] 的失败是**已记录的 app 缺陷**（见报告），不是本脚本或环境的问题。")
        leftover = [check for check in failed if not check.known_issue]
        if leftover:
            print(f"其中 {len(leftover)} 项是未记录的问题，需要重点排查。")
        print(f"core stderr 尾部（{STDERR_TAIL} 行）：")
        for line in stderr.tail(STDERR_TAIL) if stderr else []:
            print(f"  {line}")
        return 1
    print("全部 PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
