"""跨实例演练：**两个真实网关进程** + Redis，验证 SSE 扇出与断线重连补发。

为什么需要它：`docs/deployment-multi-instance.md` 的「多副本语义的对外保证」表里，**"两个 OS 进程"
这一格此前没有自动化** —— 已有的是进程内双 hub（`internal/api/sse_cross_instance_live_test.go`）
与沙箱服务的跨进程（`tests/test_sandbox_service_multiprocess.py`），都不是"两个网关进程"。

做法（不依赖引擎）：
1. 起网关 A(PORT=8081) 与 B(PORT=8082)，同一套 `.env` 密钥、同一个 Redis/PG；
2. 自签一枚 JWT（HS256 + `.env` 里的 `JWT_SECRET`）—— **两个进程都接受它**这件事本身就是多副本前提；
3. 对**同一 session** 分别在 A、B 上开 SSE（`GET /v1/events`）；
4. 直接往 Redis 通道 `subagent:events` 发一条事件（网关侧中继把它转投各自 hub，
   见 `internal/api/subagent_events_relay.go`）⇒ 断言**两个进程的客户端都收到**（跨实例扇出）；
5. 断开 A、再发两条、带 `Last-Event-ID` 重连 A ⇒ 断言**补发缺口**（跨实例重连补发）。

用法（需要先起 Redis 并构建好网关二进制）：
    python python-engine/tests/drills/multi_instance_drill.py /path/to/gateway.exe
"""

from __future__ import annotations

import http.client
import json
import os
import pathlib
import socket
import subprocess
import sys
import time
import uuid

import jwt as pyjwt
import redis

REPO = pathlib.Path(__file__).resolve().parents[3]
CHANNEL = "subagent:events"


def load_env() -> dict[str, str]:
    """配置来源：**环境变量优先**，仓库根 `.env` 仅作本地兜底（CI 里没有 `.env`）。

    取值优先级 = `os.environ` > `.env`；`DRILL_IGNORE_DOTENV=1` 可强制"只用环境变量"
    （本地验证 CI 路径时用，免得本地 `.env` 把缺失项悄悄补齐）。
    """
    file_env: dict[str, str] = {}
    dotenv = REPO / ".env"
    if dotenv.exists() and os.environ.get("DRILL_IGNORE_DOTENV") != "1":
        for line in dotenv.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                key, value = line.split("=", 1)
                file_env[key.strip()] = value.strip().strip('"')
    merged = {**file_env, **{k: v for k, v in os.environ.items() if v}}
    missing = [k for k in ("POSTGRES_DSN",) if not merged.get(k)]
    if missing:
        raise AssertionError(f"缺少必需配置：{missing}（可用 .env 或环境变量提供）")
    if not (merged.get("JWT_SECRET") or merged.get("APP_SECRET")):
        raise AssertionError("缺少 JWT_SECRET / APP_SECRET：无法签出网关认可的令牌")
    return merged


def redis_addr(config: dict[str, str]) -> tuple[str, int]:
    """Redis 地址：DRILL_REDIS_ADDR > REDIS_URL > 本机默认 6390（与开发脚本一致）。"""
    raw = os.environ.get("DRILL_REDIS_ADDR", "")
    if not raw:
        url = config.get("REDIS_URL") or os.environ.get("REDIS_URL", "")
        if url:
            from urllib.parse import urlparse

            parsed = urlparse(url)
            if parsed.hostname:
                return parsed.hostname, int(parsed.port or 6379)
    if ":" in raw:
        host, _, port = raw.partition(":")
        return host, int(port)
    if raw:
        return raw, 6379
    return "127.0.0.1", 6390


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def mint_token(secret: str, tenant_id: str = "t-drill") -> str:
    now = int(time.time())
    claims = {
        "uid": "u-drill",
        "email": "drill@example.com",
        "role": "owner",
        "tenant_id": tenant_id,
        "perms": [],
        "iat": now,
        "nbf": now,
        "exp": now + 3600,
        "iss": "chiron",
        "jti": uuid.uuid4().hex,
    }
    return pyjwt.encode(claims, secret, algorithm="HS256")


class SSE:
    """最小 SSE 客户端：按行读，带截止时间，能回报收到的 (id, event, data)。"""

    def __init__(self, port: int, session_id: str, client_id: str, token: str) -> None:
        self.conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
        path = f"/v1/events?session_id={session_id}&client_id={client_id}"
        self.conn.request(
            "GET", path, headers={"Authorization": f"Bearer {token}", "Accept": "text/event-stream"}
        )
        self.resp = self.conn.getresponse()
        self.status = self.resp.status
        if self.status != 200:
            raise AssertionError(f"SSE 连接失败：HTTP {self.status} {self.resp.read()[:200]!r}")
        self._buf = {"id": "", "event": "", "data": ""}

    def next_event(self, timeout: float = 8.0) -> dict | None:
        deadline = time.monotonic() + timeout
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return None
            assert self.conn.sock is not None
            self.conn.sock.settimeout(remaining)
            try:
                raw = self.resp.fp.readline()  # type: ignore[union-attr]
            except TimeoutError:
                return None
            except OSError:
                return None
            if not raw:
                return None
            line = raw.decode("utf-8", "replace").rstrip("\r\n")
            if line == "":
                if self._buf["data"] or self._buf["event"]:
                    ev, self._buf = self._buf, {"id": "", "event": "", "data": ""}
                    return ev
                continue
            if line.startswith(":"):  # 心跳/注释
                continue
            for field in ("id", "event", "data"):
                prefix = f"{field}:"
                if line.startswith(prefix):
                    value = line[len(prefix):].lstrip()
                    self._buf[field] = value
                    break

    def close(self) -> None:
        try:
            self.conn.close()
        except Exception:  # noqa: BLE001
            pass


def wait_health(port: int, timeout: float = 30.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            conn = http.client.HTTPConnection("127.0.0.1", port, timeout=1)
            conn.request("GET", "/health")
            if conn.getresponse().status == 200:
                return
        except Exception:  # noqa: BLE001
            pass
        time.sleep(0.3)
    raise AssertionError(f"网关 {port} 在 {timeout}s 内未就绪")


def parse_frame(ev: dict) -> dict:
    """把 SSE 帧还原成 payload。

    网关发的 `data` 是**信封**：`{"id","type","data":{...原始 payload...},"session_id"}`，
    即原始 payload 嵌在 `data.data` 里（探针实测）。这里把两层合起来，调用方不必关心。
    """
    raw = ev.get("data") or ""
    try:
        envelope = json.loads(raw)
    except ValueError:
        return {}
    if not isinstance(envelope, dict):
        return {}
    inner = envelope.get("data")
    merged = {**envelope}
    if isinstance(inner, dict):
        merged.update(inner)
    return merged


def wait_for_seq(sse: SSE, seq: int, timeout: float = 8.0) -> tuple[dict, dict] | None:
    """读到我们发布的那条（SSE 首帧通常是 `connected` 心跳，不能拿第一帧断言）。"""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        ev = sse.next_event(timeout=max(0.1, deadline - time.monotonic()))
        if ev is None:
            return None
        payload = parse_frame(ev)
        if payload.get("seq") == seq:
            return ev, payload
    return None


def publish(rdb: redis.Redis, session_id: str, seq: int) -> None:
    payload = {
        "type": "subagent_status",
        "session_id": session_id,
        "run_id": "run-drill",
        "status": "running",
        "seq": seq,
    }
    rdb.publish(CHANNEL, json.dumps(payload))


def main() -> int:
    if len(sys.argv) < 2:
        print("用法：python tests/drills/multi_instance_drill.py <gateway-binary>")
        return 2
    ok = False
    binary = sys.argv[1]
    env_file = load_env()
    redis_host, redis_port = redis_addr(env_file)

    base_env = {
        **os.environ,
        **env_file,
        "REDIS_ADDR": f"{redis_host}:{redis_port}",
    }
    ports = [free_port(), free_port()]
    while ports[1] == ports[0]:  # 极小概率撞端口
        ports[1] = free_port()
    procs: list[subprocess.Popen] = []
    logs: list[pathlib.Path] = []
    handles: list = []
    session_id = f"drill-{uuid.uuid4().hex[:10]}"
    log_dir = pathlib.Path(os.environ.get("TEMP", ".")) / f"chiron-drill-{uuid.uuid4().hex[:6]}"
    log_dir.mkdir(parents=True, exist_ok=True)

    try:
        for index, port in enumerate(ports):
            # ⚠ 网关日志很多：**不要用 PIPE** —— 没人读的话管道写满会把进程卡死在启动阶段
            # （本轮实测：两个网关 30s 都没起来）。写文件即可。
            log_path = log_dir / f"gw-{port}.log"
            logs.append(log_path)
            handle = log_path.open("w", encoding="utf-8")
            handles.append(handle)
            procs.append(
                subprocess.Popen(
                    [binary],
                    cwd=str(REPO),
                    env={**base_env, "PORT": str(port), "WORKER_ID": str(index)},
                    stdout=handle,
                    stderr=subprocess.STDOUT,
                    text=True,
                )
            )
        for port in ports:
            wait_health(port)
        print(f"两个网关已就绪：{ports}；session={session_id}")

        token = mint_token(env_file.get("JWT_SECRET") or env_file["APP_SECRET"])
        rdb = redis.Redis(host=redis_host, port=redis_port, decode_responses=True)

        # ── ① 两个进程各自订阅同一 session，一次发布两边都该收到 ──
        a = SSE(ports[0], session_id, "probe-a", token)
        b = SSE(ports[1], session_id, "probe-b", token)
        time.sleep(1.0)  # 让两侧订阅生效
        publish(rdb, session_id, 1)
        hit_a, hit_b = wait_for_seq(a, 1), wait_for_seq(b, 1)
        assert hit_a is not None, "A 进程没收到跨实例事件（扇出失败）"
        assert hit_b is not None, "B 进程没收到跨实例事件（扇出失败）"
        (ev_a, _pay_a), (_ev_b, _pay_b) = hit_a, hit_b
        for name, payload in (("A", _pay_a), ("B", _pay_b)):
            assert payload.get("session_id") == session_id, f"{name} 收到串扰：{payload}"
            assert payload.get("seq") == 1, f"{name} 收到的事件不对：{payload}"
        print(f"✅ 跨实例扇出：A 与 B 都收到 seq=1（event={ev_a['event']!r}, id={ev_a['id']}）")

        # ── ①b 实时**不重复**：修复前每个实例还会把事件跨实例再广播一次 ⇒ 客户端收到 2 份 ──
        extra = 0
        quiet_until = time.monotonic() + 1.2
        while time.monotonic() < quiet_until:
            ev = a.next_event(timeout=max(0.05, quiet_until - time.monotonic()))
            if ev is None:
                break
            if parse_frame(ev).get("seq") == 1:
                extra += 1
        assert extra == 0, f"实时投递重复了：额外收到 {extra} 份 seq=1（跨实例再广播未消除）"
        print("✅ 实时不重复：seq=1 每个客户端恰好一份")

        # ── ② 断开 A → 期间发两条 → 带 Last-Event-ID 重连应补发缺口 ──
        last_id = ev_a["id"]
        a.close()
        time.sleep(0.3)
        publish(rdb, session_id, 2)
        publish(rdb, session_id, 3)
        time.sleep(0.5)

        a2 = SSE.__new__(SSE)  # 直接构造带 Last-Event-ID 的连接（浏览器 EventSource 自动重连就是这么带的）
        a2.conn = http.client.HTTPConnection("127.0.0.1", ports[0], timeout=10)
        a2.conn.request(
            "GET",
            f"/v1/events?session_id={session_id}&client_id=probe-a2",
            headers={
                "Authorization": f"Bearer {token}",
                "Accept": "text/event-stream",
                "Last-Event-ID": last_id,
            },
        )
        a2.resp = a2.conn.getresponse()
        assert a2.resp.status == 200, f"重连失败：HTTP {a2.resp.status}"
        a2._buf = {"id": "", "event": "", "data": ""}

        replayed: list[int] = []
        deadline = time.monotonic() + 8
        while time.monotonic() < deadline and len(replayed) < 4:
            ev = a2.next_event(timeout=max(0.1, deadline - time.monotonic()))
            if ev is None:
                break
            payload = parse_frame(ev)
            if payload.get("seq") in (2, 3):
                replayed.append(int(payload["seq"]))
        assert {2, 3} <= set(replayed), f"重连补发缺口未补齐：收到 {replayed}（期望含 2 与 3）"
        print(f"✅ 跨实例重连补发：断线期间的 seq=2,3 已补发（{replayed}）")

        # ── ③ 回归护栏：**多实例下补发不重复**（2026-10-08 修复）──
        #
        # 修复前：引擎把子 Agent 事件发到 Redis 通道，**每个网关实例的中继**都 `hub.Publish`，
        # 而 Publish 既追加共享重放流**又跨实例再广播** ⇒ 补发 [2,2,3,3]、实时也各收两份。
        # 修复：中继改走 `RelayEvent`（只追加本实例流 + 本地 fanout，**不再跨实例广播**），
        # 写入端不做互斥（保住每个客户端拿到的 `id`），而在**读取端**（`ReplayAfter`）
        # 按逻辑身份（`event_id`，退化用 payload 哈希）去重。
        duplicates = len(replayed) - len(set(replayed))
        expected_duplicates = 0
        print(
            f"✅ 补发不重复：收到 {replayed}，重复 {duplicates} 条（修复前为 2；"
            "回归护栏见 internal/broadcast/hub_live_test.go::TestLiveReplayDedupesSameLogicalEventAcrossInstances）"
        )
        assert duplicates == expected_duplicates, (
            f"补发出现重复：{duplicates} 条（{replayed}）—— 多实例去重被破坏了，"
            "检查 internal/broadcast/hub.go 的 logicalEventID / RelayEvent"
        )
        a2.close()
        ok = True
        return 0
    finally:
        for proc in procs:
            if proc.poll() is None:
                proc.kill()
        for handle in handles:
            try:
                handle.close()
            except Exception:  # noqa: BLE001
                pass
        # 只在失败时回吐网关日志（成功时保持输出干净，也避免把正常日志当错误上报）
        if not ok:
            for path in logs:
                try:
                    tail = path.read_text(encoding="utf-8", errors="replace").strip()
                except Exception:  # noqa: BLE001
                    tail = "(读取失败)"
                if tail:
                    print(f"--- {path.name} 末尾输出 ---\n{tail[-1500:]}", file=sys.stderr)


if __name__ == "__main__":
    sys.exit(main())
