"""Redis 适配层：优先 ``fakeredis``（不引入二进制），可选真实 Redis / Valkey。

契约 §5.3 要求**逐项实测** Streams / Pub-Sub 的支持情况，因此这里不做任何"应该支持"的
推断：每个命令都真跑一次，把异常类型与消息原样带出来（:class:`ProbeResult`）。

后端选择：

1. ``CHIRON_LOCAL_REDIS_URL`` 有值 → 连真实服务（Redis 6.2+ / Valkey 7+ 的
   XAUTOCLAIM 才存在），后端名记成 ``server``。
2. 否则 → ``fakeredis``（纯 Python，无二进制）。缺口会原样暴露在 ``probe()`` 结果里。

降级路线（不往 core 里塞二进制）：

- 换外部 Valkey：``docker run --rm -p 6379:6379 valkey/valkey:8`` +
  ``CHIRON_LOCAL_REDIS_URL=redis://127.0.0.1:6379/0``。
- 缩小 core 范围：不依赖 Streams 消费组，改用 PG 表队列 / 轮询（业务层第二批判定）。
"""

from __future__ import annotations

import asyncio
import importlib
import time
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from app.config import Settings

PROBE_OPS: tuple[str, ...] = (
    "XADD",
    "XREADGROUP",
    "XACK",
    "XCLAIM",
    "XAUTOCLAIM",
    "PUBLISH",
    "SUBSCRIBE",
)

VALKEY_HINT = (
    "备选方案（不引入二进制，只连外部服务）："
    "(1) docker run --rm -p 6379:6379 valkey/valkey:8 并设置 "
    "CHIRON_LOCAL_REDIS_URL=redis://127.0.0.1:6379/0；"
    "(2) 或缩小 core 范围：不依赖 Streams 消费组，改用 PG 表队列 / 轮询"
)


@dataclass
class ProbeResult:
    """一条命令的实测结果。``detail`` 是实测到的原始返回或原始异常。"""

    op: str
    ok: bool
    detail: str
    elapsed_ms: float = 0.0

    def as_dict(self) -> dict[str, Any]:
        return {
            "op": self.op,
            "status": "PASS" if self.ok else "FAIL",
            "detail": self.detail,
            "elapsed_ms": self.elapsed_ms,
        }

    def line(self) -> str:
        mark = "PASS" if self.ok else "FAIL"
        return f"  {self.op:<11} {mark}  {self.detail}"


class RedisUnavailable(RuntimeError):  # noqa: N818 —— 名字表达“不可用”而非“错误”
    """既没有 fakeredis 也没有可连的外部 Redis。"""


class RedisServer:
    """Redis 客户端生命周期 + 命令级探测。"""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.client: Any = None
        self.backend = "none"
        self.error = ""
        self.warning = ""
        self._pubsubs: list[Any] = []

    # ------------------------------------------------------------------ 启停
    async def start(self) -> None:
        if not self.settings.redis_enabled:
            self.backend = "disabled"
            self.error = "CHIRON_LOCAL_REDIS_ENABLED=false"
            return

        if self.settings.redis_url:
            try:
                import redis.asyncio as redis_asyncio

                self.client = redis_asyncio.from_url(self.settings.redis_url, decode_responses=True)
                await self.client.ping()
                self.backend = "server"
                self.error = ""
                return
            except Exception as exc:  # noqa: BLE001 —— 外部服务连不上就退到 fakeredis
                self.warning = f"外部 Redis 不可用（{type(exc).__name__}: {exc}）；{VALKEY_HINT}"
                self.client = None

        try:
            self.client = self._build_fakeredis()
            await self.client.ping()
            self.backend = "fakeredis"
            self.error = ""
        except Exception as exc:  # noqa: BLE001
            self.client = None
            self.backend = "unavailable"
            self.error = f"fakeredis 初始化失败（{type(exc).__name__}: {exc}）；{VALKEY_HINT}"

    def _build_fakeredis(self) -> Any:
        """兼容 fakeredis 2.x/3.x 的两种异步入口写法。"""
        candidates = (
            ("fakeredis.aioredis", "FakeRedis"),
            ("fakeredis", "FakeAsyncRedis"),
            ("fakeredis.aioredis", "FakeAsyncRedis"),
        )
        errors: list[str] = []
        for module_path, attr in candidates:
            try:
                module = importlib.import_module(module_path)
            except ImportError as exc:
                errors.append(f"{module_path}: ImportError: {exc}")
                continue
            factory = getattr(module, attr, None)
            if factory is None:
                errors.append(f"{module_path}.{attr}: 不存在")
                continue
            try:
                return factory(decode_responses=True)
            except TypeError:
                return factory()
            except Exception as exc:  # noqa: BLE001
                errors.append(f"{module_path}.{attr}: {type(exc).__name__}: {exc}")
        raise RedisUnavailable(
            "fakeredis 未安装或不兼容：" + "; ".join(errors) + f"；{VALKEY_HINT}"
        )

    async def stop(self) -> None:
        for pubsub in self._pubsubs:
            try:
                await pubsub.aclose()
            except Exception:  # noqa: BLE001
                try:
                    await pubsub.close()
                except Exception:  # noqa: BLE001
                    pass
        self._pubsubs.clear()
        client = self.client
        self.client = None
        if client is not None:
            closer = getattr(client, "aclose", None) or getattr(client, "close", None)
            if callable(closer):
                result = closer()
                if asyncio.iscoroutine(result):
                    await result

    # ------------------------------------------------------------------ 健康
    async def health(self) -> dict[str, Any]:
        result: dict[str, Any] = {
            "ok": False,
            "backend": self.backend,
            "url": self.settings.redis_url,
            "error": self.error,
            "warning": self.warning,
        }
        if self.client is None:
            return result
        try:
            pong = await self.client.ping()
            result["ok"] = bool(pong)
        except Exception as exc:  # noqa: BLE001
            result["error"] = f"{type(exc).__name__}: {exc}"
        return result

    # ------------------------------------------------------------------ 探测
    async def probe(self, prefix: str = "chiron:p0") -> list[ProbeResult]:
        """逐项实测契约 §5.3 的命令，返回**每一条**的结果（含失败原因）。"""
        if self.client is None:
            return [ProbeResult(op, False, f"客户端未就绪：{self.error or '未启动'}") for op in PROBE_OPS]

        return [
            await self._probe_xadd(prefix),
            await self._probe_xreadgroup(prefix),
            await self._probe_xack(prefix),
            await self._probe_xclaim(prefix),
            await self._probe_xautoclaim(prefix),
            await self._probe_publish(prefix),
            await self._probe_subscribe(prefix),
        ]

    async def _measure(self, op: str, fn: Callable[[], Awaitable[str]]) -> ProbeResult:
        started = time.perf_counter()
        try:
            detail = await fn()
            ok = True
        except Exception as exc:  # noqa: BLE001 —— 失败原因必须原样上报
            detail = f"{type(exc).__name__}: {exc}"
            ok = False
        return ProbeResult(op=op, ok=ok, detail=detail, elapsed_ms=round((time.perf_counter() - started) * 1000, 1))

    def _keys(self, prefix: str, op: str) -> tuple[str, str, str]:
        token = uuid.uuid4().hex[:8]
        return (f"{prefix}:{op}:{token}", f"{prefix}:{op}:{token}:g", f"c-{token}")

    async def _seed_group(self, stream: str, group: str, consumer: str) -> str:
        """造一条 pending 条目（XCLAIM / XAUTOCLAIM 需要），返回消息 id。"""
        message_id = await self.client.xadd(stream, {"seed": "1"})
        try:
            await self.client.xgroup_create(stream, group, id="0")
        except Exception as exc:  # noqa: BLE001
            # 消费组已存在（BUSYGROUP）不算错；其它错误往上抛
            if "BUSYGROUP" not in str(exc):
                raise
        await self.client.xreadgroup(group, consumer, {stream: ">"}, count=1)
        return str(message_id)

    async def _probe_xadd(self, prefix: str) -> ProbeResult:
        stream, _group, _consumer = self._keys(prefix, "xadd")

        async def run() -> str:
            message_id = await self.client.xadd(stream, {"step": "xadd"})
            return f"stream={stream} id={message_id}"

        return await self._measure("XADD", run)

    async def _probe_xreadgroup(self, prefix: str) -> ProbeResult:
        stream, group, consumer = self._keys(prefix, "xreadgroup")

        async def run() -> str:
            message_id = await self._seed_group(stream, group, consumer)
            entries = await self.client.xreadgroup(group, consumer, {stream: ">"}, count=1)
            count = sum(len(items) for _name, items in entries) if entries else 0
            return f"group={group} seed_id={message_id} 读到 {count} 条"

        return await self._measure("XREADGROUP", run)

    async def _probe_xack(self, prefix: str) -> ProbeResult:
        stream, group, consumer = self._keys(prefix, "xack")

        async def run() -> str:
            message_id = await self._seed_group(stream, group, consumer)
            acked = await self.client.xack(stream, group, message_id)
            return f"ack({message_id}) → {acked}"

        return await self._measure("XACK", run)

    async def _probe_xclaim(self, prefix: str) -> ProbeResult:
        stream, group, consumer = self._keys(prefix, "xclaim")

        async def run() -> str:
            message_id = await self._seed_group(stream, group, consumer)
            claimed = await self.client.xclaim(stream, group, "c-claimer", 0, [message_id])
            return f"xclaim({message_id}) → {len(claimed or [])} 条"

        return await self._measure("XCLAIM", run)

    async def _probe_xautoclaim(self, prefix: str) -> ProbeResult:
        stream, group, consumer = self._keys(prefix, "xautoclaim")

        async def run() -> str:
            message_id = await self._seed_group(stream, group, consumer)
            outcome = await self.client.xautoclaim(stream, group, "c-auto", 0, start_id="0-0", count=10)
            claimed = outcome[1] if isinstance(outcome, (list, tuple)) and len(outcome) > 1 else []
            return f"seed_id={message_id} xautoclaim → {len(claimed or [])} 条"

        return await self._measure("XAUTOCLAIM", run)

    async def _probe_publish(self, prefix: str) -> ProbeResult:
        channel = f"{prefix}:publish:{uuid.uuid4().hex[:8]}"

        async def run() -> str:
            subscribers = await self.client.publish(channel, "hello")
            return f"channel={channel} subscribers={subscribers}"

        return await self._measure("PUBLISH", run)

    async def _probe_subscribe(self, prefix: str) -> ProbeResult:
        channel = f"{prefix}:subscribe:{uuid.uuid4().hex[:8]}"

        async def run() -> str:
            pubsub = self.client.pubsub()
            self._pubsubs.append(pubsub)
            await pubsub.subscribe(channel)
            await asyncio.sleep(0.05)
            await self.client.publish(channel, "hello")
            deadline = time.monotonic() + 2.0
            while time.monotonic() < deadline:
                message = await pubsub.get_message(ignore_subscribe_messages=True, timeout=0.2)
                if message and message.get("type") == "message":
                    return f"channel={channel} 收到 {message.get('data')!r}"
            raise TimeoutError("订阅后 2s 内未收到消息")

        return await self._measure("SUBSCRIBE", run)
