"""真实 PostgreSQL 上的本地 core 集成测试夹具。

与 ``tests/conftest.py`` 的区别：那份把 PG / Redis 全关、把 ``asyncpg`` 换成替身，只验证
HTTP 形状；这份起**真实** PostgreSQL 实例（后端由 :mod:`app.storage.pg` 自己挑：
external / embedded 包 / 官方二进制），跑真实 ``alembic upgrade head``，再让
``create_app`` 连它。

三条硬规矩：

1. **起不来就 skip**，绝不伪造通过 —— ``pytest.skip`` 的文案里带真实的异常类型与消息。
2. 推理走本地 mock HTTP 服务（标准库 ``http.server``）：core 仍按契约把请求发到
   ``CHIRON_LOCAL_INFERENCE_PROXY``，链路是真的，只是"模型"换成固定回复。
3. 数据目录、端口、媒体文件全在 pytest 的 ``tmp_path`` 下，不碰仓库里任何用户数据。

关于 ``run_migrations``：PG 由 :func:`pg_server` 一次性迁移，``create_app`` 收到的
配置里 ``run_migrations=False`` —— 每个测试重复跑 ``alembic upgrade head`` 只是白等
十几秒，不增加任何覆盖；迁移本身由 ``test_local_e2e.py::test_alembic_head_is_applied``
在真实库里复核。
"""

from __future__ import annotations

import json
import shutil
import socket
import threading
import time
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import pytest
from app.config import Settings
from app.knowledge import ENV_FLAG as KNOWLEDGE_ENV_FLAG
from app.main import create_app
from app.storage.pg import PostgresServer
from fastapi.testclient import TestClient

#: mock 推理服务的固定回复（``content`` 用于断言"回复真的来自代理"）
MOCK_CONTENT = "mock reply"

#: 与 ``docs/desktop-local/contracts.md`` §9 一致的响应形状。
#: 注意 ``usage`` 用的是 ``input_tokens`` / ``output_tokens`` —— 这正是报告里
#: app bug #1 的证据：``app/runtime/inference.py::_normalize_usage`` 只读
#: ``prompt_tokens`` / ``completion_tokens``。
MOCK_INFERENCE_REPLY: dict[str, Any] = {
    "content": MOCK_CONTENT,
    "usage": {"input_tokens": 1, "output_tokens": 1},
    "provider": "mock",
    "model": "mock",
}

PNG_BYTES = b"\x89PNG\r\n\x1a\n" + b"chiron-local-integration"


# --------------------------------------------------------------------- mock 推理服务
class MockInferenceServer:
    """本地 mock 推理服务：``POST /infer`` 返回固定 JSON，并把收到的请求记在 ``calls``。"""

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
                    body = {"_unparsable": raw[:200].decode("utf-8", "replace")}
                if self.path != "/infer":
                    self._reply(404, {"error": "not_found", "path": self.path})
                    return
                outer.calls.append(
                    {
                        "path": self.path,
                        "body": body,
                        "authorization": self.headers.get("Authorization", ""),
                    }
                )
                self._reply(200, MOCK_INFERENCE_REPLY)

            def _reply(self, status: int, payload: dict[str, Any]) -> None:
                data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
                self.send_response(status)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def log_message(self, fmt: str, *args: Any) -> None:
                """静音：mock 服务的访问日志会淹没 pytest 输出。"""

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self._thread = threading.Thread(target=self._server.serve_forever, name="mock-inference", daemon=True)

    # ------------------------------------------------------------------ 生命周期
    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._server.shutdown()
        self._server.server_close()
        self._thread.join(timeout=5)

    def reset(self) -> None:
        self.calls.clear()

    @property
    def base_url(self) -> str:
        host, port = self._server.server_address[:2]
        return f"http://{host}:{port}"


# --------------------------------------------------------------------- 端口工具
def free_port(host: str = "127.0.0.1") -> int:
    """拿一个"刚被释放、大概率无人监听"的端口（用于构造连不上的 DSN）。"""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind((host, 0))
        return int(sock.getsockname()[1])


def make_settings(data_dir: Path, **overrides: Any) -> Settings:
    """构造配置并**禁用 ``.env``**（``_env_file=None``）。

    为什么必须禁用：仓库根的 ``.env`` 可能带 ``POSTGRES_DSN`` 之类的键，
    ``postgres_dsn`` 字段的 ``validation_alias`` 也认 ``CHIRON_LOCAL_POSTGRES_DSN``；
    一旦被外部文件改掉，测试就会连到开发库上，而不是自己起的实例。
    """
    defaults: dict[str, Any] = {
        "data_dir": data_dir,
        "pg_enabled": True,
        "redis_enabled": False,  # fakeredis 是可选依赖，集成测试不依赖它
        "run_migrations": False,
    }
    defaults.update(overrides)
    return Settings(_env_file=None, **defaults)


# --------------------------------------------------------------------- fixtures
@pytest.fixture(scope="session")
def pg_server(tmp_path_factory: pytest.TempPathFactory) -> Iterator[PostgresServer]:
    """起真实 PostgreSQL 并跑 ``alembic upgrade head``；起不来就 skip（附真实错误）。"""
    base = tmp_path_factory.mktemp("chiron-local-integration")
    settings = make_settings(base / "data", run_migrations=True)
    server = PostgresServer(settings)
    try:
        dsn = server.start()
        migration = server.migrate(dsn)
    except Exception as exc:  # noqa: BLE001 —— 任何一条路失败都只说明"本机起不来"
        server.stop()
        pytest.skip(f"真实 PostgreSQL 不可用，集成测试不伪造通过：{type(exc).__name__}: {str(exc)[:2000]}")
    print(
        f"\n[integration] PG backend={server.backend} dsn={server.safe_dsn} "
        f"revision={migration.get('revision')} migrate={migration.get('elapsed_ms')}ms"
    )
    try:
        yield server
    finally:
        server.stop()
        shutil.rmtree(base, ignore_errors=True)


@pytest.fixture(scope="session")
def pg_dsn(pg_server: PostgresServer) -> str:
    """真实实例的 DSN（测试用它直接跑 SQL 做旁证）。"""
    return pg_server.dsn


@pytest.fixture(scope="session")
def mock_inference() -> Iterator[MockInferenceServer]:
    """本地 mock 推理服务（``POST /infer``）。"""
    server = MockInferenceServer()
    server.start()
    try:
        yield server
    finally:
        server.stop()


@pytest.fixture
def api_settings(tmp_path: Path, pg_dsn: str, mock_inference: MockInferenceServer) -> Settings:
    """指向**已启动的真实 PG** + mock 推理代理的配置。"""
    return make_settings(
        tmp_path / "data",
        postgres_dsn=pg_dsn,
        inference_proxy=mock_inference.base_url,
        # 心跳压到 10ms 且只发 2 条，SSE 测试不用等 15s
        sse_heartbeat_seconds=0.01,
        sse_max_heartbeats=2,
    )


@pytest.fixture
def app_client(api_settings: Settings) -> Iterator[TestClient]:
    """完整 app（所有 router 都已注册）跑在真实 PG 上。

    存储初始化是**后台 task**（``app/main.py::lifespan``）：进入 TestClient 上下文时
    startup 已返回，bootstrap 可能仍在跑。这里等到 ``storage_ready`` 再交给用例，
    让"每一条断言都发生在存储就绪之后"成为夹具的保证，而不是散落在各用例里。
    """
    with TestClient(create_app(api_settings)) as client:
        wait_for_storage(client)
        yield client


def wait_for_storage(client: TestClient, timeout: float = 30.0) -> dict[str, Any]:
    """轮询 ``/readyz`` 直到离开 ``starting``，返回最后一次 payload。

    超时直接抛：静默放行会让后续断言在"存储还没起来"的状态下做出错误结论。
    """
    deadline = time.monotonic() + timeout
    payload: dict[str, Any] = {}
    while time.monotonic() < deadline:
        payload = client.get("/readyz").json()
        if payload.get("status") != "starting":
            return payload
        time.sleep(0.05)
    raise AssertionError(f"存储初始化 {timeout}s 内没有结束：{payload}")


@pytest.fixture
def knowledge_settings(api_settings: Settings, monkeypatch: pytest.MonkeyPatch) -> Settings:
    """打开知识库开关（``CHIRON_LOCAL_KNOWLEDGE_ENABLED=1``）的配置。

    开关是**每次请求现读环境变量**（``app/knowledge/__init__.py::is_enabled``），
    所以不需要为它重启 app。
    """
    monkeypatch.setenv(KNOWLEDGE_ENV_FLAG, "1")
    return api_settings


@pytest.fixture
def knowledge_client(knowledge_settings: Settings) -> Iterator[TestClient]:
    with TestClient(create_app(knowledge_settings)) as client:
        yield client


@pytest.fixture
def unreachable_dsn() -> str:
    """一个**必定连不上**的 DSN（端口刚被释放，同机无人监听）。"""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        port = int(sock.getsockname()[1])
    return f"postgresql://chiron:chiron@127.0.0.1:{port}/chiron"


@pytest.fixture
def down_client(tmp_path: Path, unreachable_dsn: str) -> Iterator[TestClient]:
    """PG 连不上、但 ``app.state.pg.dsn`` 非空的 app（真实连接被拒，不是替身）。

    ``PostgresServer.start()`` 走 external 分支时会**先**把 ``_dsn`` 设成用户给的 DSN，
    再尝试 ``SELECT 1``；失败后 ``/readyz`` 报错，但 ``pg.dsn`` 仍然非空 ——
    这正是"DSN 在、库没了"的现实场景。
    """
    settings = make_settings(tmp_path / "down", postgres_dsn=unreachable_dsn, inference_proxy="")
    app = create_app(settings)
    # raise_server_exceptions=False：让未捕获异常也变成真实 500 响应，
    # 这样"该 503 却 500"的路径能被断言出来（见报告里的 app bug #3）。
    with TestClient(app, raise_server_exceptions=False) as client:
        # 存储初始化在后台 task 里；等它结束再看 storage_errors，
        # 否则会读到"还没开始跑"的空列表。
        wait_for_storage(client)
        assert app.state.storage_errors, "PG 连不上却没记录 storage_errors"
        yield client


__all__ = [
    "KNOWLEDGE_ENV_FLAG",
    "MOCK_CONTENT",
    "MOCK_INFERENCE_REPLY",
    "PNG_BYTES",
    "MockInferenceServer",
    "free_port",
    "make_settings",
]
