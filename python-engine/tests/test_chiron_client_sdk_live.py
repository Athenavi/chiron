"""客户端 SDK 对**真实网关**的端到端验证（integration）。

单元用例证明"请求怎么发的、帧怎么解析的"；这一条证明**真网关认得它**：
注册/登录拿到令牌 → 订阅 `GET /v1/events` → 事件（由 Redis 通道注入，模拟引擎侧广播）
→ 客户端真的收到并解析出业务负载 → 取消不 5xx。

跳过条件：网关不可达，或实例关闭了注册（`DISABLE_REGISTRATION`）。
CI 的 real-stack job 里网关就在 127.0.0.1:8080，这条会真跑。
"""

from __future__ import annotations

import json
import os
import pathlib
import sys
import threading
import time
import urllib.request
import uuid

import pytest

REPO = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "clients" / "python"))

from chiron_client import ChironClient, ChironError  # noqa: E402

GATEWAY = os.environ.get("CHIRON_GATEWAY_URL", "http://127.0.0.1:8080").rstrip("/")


def _gateway_alive() -> bool:
    try:
        with urllib.request.urlopen(f"{GATEWAY}/health", timeout=2) as resp:
            return resp.status == 200
    except Exception:  # noqa: BLE001
        return False


def _mint_token() -> str | None:
    """本地/CI 里没有可用账号时，用网关认的密钥自签一枚令牌（与跨实例演练同一手法）。

    这样"注册被关 / 要求邮箱验证码"的实例上，SSE 与取消这两条**仍然能被真实验证**。
    """
    secret = os.environ.get("JWT_SECRET") or os.environ.get("APP_SECRET") or ""
    if not secret:
        return None
    try:
        import jwt as pyjwt
    except ImportError:  # pragma: no cover - 开发依赖里已声明 PyJWT
        return None
    now = int(time.time())
    claims = {
        "uid": "sdk-live-user",
        "email": "sdk-live@example.com",
        "role": "owner",
        "tenant_id": "default",
        "perms": [],
        "iat": now,
        "nbf": now,
        "exp": now + 3600,
        "iss": "chiron",
        "jti": uuid.uuid4().hex,
    }
    return pyjwt.encode(claims, secret, algorithm="HS256")


@pytest.mark.integration
def test_client_against_real_gateway():
    if not _gateway_alive():
        pytest.skip(f"网关不可达（{GATEWAY}）：跳过 SDK 端到端用例")

    client = ChironClient(GATEWAY, timeout=10)
    email = f"sdk-{uuid.uuid4().hex[:10]}@example.com"
    auth_path = "register+login"
    try:
        registered = client.register(email, "SdkTest-Password-123", name="SDK Test")
        if not str(registered.get("token") or ""):
            client.login(email, "SdkTest-Password-123")
    except ChironError as exc:
        # 实例可能关了注册 / 要求邮箱验证码 —— 退化为自签令牌，SSE 与取消仍然真实验证
        token = _mint_token()
        if not token:
            pytest.skip(f"注册不可用（{exc}）且无 JWT_SECRET/APP_SECRET 可自签：跳过")
        client.token = token
        auth_path = f"minted-token（注册不可用：{exc}）"
    assert client.token, "必须持有令牌"
    print(f"[sdk-live] 认证路径：{auth_path}")

    # ── 事件流：订阅真网关的 SSE，再从 Redis 通道注入一条（模拟引擎侧广播）──
    import redis

    redis_url = os.environ.get("REDIS_URL", "redis://127.0.0.1:6390/0")
    from urllib.parse import urlparse

    opts = urlparse(redis_url)
    rdb = redis.Redis(host=opts.hostname or "127.0.0.1", port=opts.port or 6379, decode_responses=True)

    session_id = f"sdk-live-{uuid.uuid4().hex[:10]}"
    received: list = []
    errors: list = []

    def _consume() -> None:
        try:
            for event in client.stream_events(session_id, timeout=15):
                received.append(event)
                if event.type == "subagent_status":
                    return
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)

    consumer = threading.Thread(target=_consume, daemon=True)
    consumer.start()
    time.sleep(0.8)  # 让 SSE 订阅先建立
    rdb.publish(
        "subagent:events",
        json.dumps({"type": "subagent_status", "session_id": session_id, "seq": 7, "run_id": "sdk-run"}),
    )
    consumer.join(timeout=20)

    assert not errors, f"客户端读取事件流报错：{errors}"
    assert received, "真网关下没有收到任何事件（订阅或解析链路断了）"
    hit = next((e for e in received if e.type == "subagent_status"), None)
    assert hit is not None, f"没收到注入的事件，实际收到：{[e.type for e in received]}"
    assert hit.session_id == session_id and hit.data.get("seq") == 7, f"事件负载不对：{hit}"
    assert hit.id, "事件必须带流 ID（断线重连靠它）"

    # ── 取消：没有运行也应"明确失败"而不是 5xx ──
    try:
        client.cancel(session_id)
    except ChironError as exc:
        assert exc.status is not None and 400 <= exc.status < 500, f"取消失败应是 4xx，实际 {exc.status}"
