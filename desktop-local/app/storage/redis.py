"""Redis 适配层：内置二进制优先，其次外部服务，最后 ``fakeredis``。

契约 §5.3 要求**逐项实测** Streams / Pub-Sub 的支持情况，因此这里不做任何"应该支持"的
推断：每个命令都真跑一次，把异常类型与消息原样带出来（:class:`ProbeResult`）。

后端选择（**顺序即优先级**）：

1. ``CHIRON_LOCAL_REDIS_URL`` 有值 → 连真实服务（Redis 6.2+ / Valkey 7+ 的
   XAUTOCLAIM 才存在），后端名记成 ``server``。
2. ``redis-server`` 二进制（``CHIRON_LOCAL_REDIS_BIN_DIR`` → 内置目录 → PATH）
   → 本进程拉起一个**独立端口**的实例，后端名记成 ``bundled``。
3. 否则 → ``fakeredis``（纯 Python，无二进制）。缺口会原样暴露在 ``probe()`` 结果里。

为什么第 2 档必须存在、且排在 fakeredis 之前：``fakeredis`` 对契约 §5.3 的七条命令
实测 6/7 通过，但 **EVAL 一条都不支持**（``unknown command 'eval'``）。而引擎的
KeyRing 仲裁（D19）、限流、预算预留、队列闸门共 15 处依赖 Redis 单键 EVAL 的
**原子性**，``vendor/规划.md:132`` 也记过「桩 Redis 不解释 Lua ⇒ 这类算术只能在真
Redis 上验」。桩能证明"命令存在"，证明不了"语义正确"，所以本地模式优先用真二进制，
并把 Lua 自检结果（:attr:`RedisServer.lua_supported`）如实上报。

降级路线（不往 core 里塞二进制时）：

- 换外部 Valkey：``docker run --rm -p 6379:6379 valkey/valkey:8`` +
  ``CHIRON_LOCAL_REDIS_URL=redis://127.0.0.1:6379/0``。
- 缩小 core 范围：不依赖 Streams 消费组，改用 PG 表队列 / 轮询（业务层第二批判定）。
"""

from __future__ import annotations

import asyncio
import contextlib
import importlib
import os
import shutil
import subprocess
import time
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from app.config import Settings
from app.storage.pg import find_free_port, wait_for_port

# 内置 redis 二进制的默认落位（相对本文件：desktop-local/app/storage/ → desktop-local/bin/redis/）
BUNDLED_REDIS_DIR = Path(__file__).resolve().parents[2] / "bin" / "redis"

PROBE_OPS: tuple[str, ...] = (
    "XADD",
    "XREADGROUP",
    "XACK",
    "XCLAIM",
    "XAUTOCLAIM",
    "PUBLISH",
    "SUBSCRIBE",
    # 契约 §5.3 的七条之外补 EVAL：本项目真正依赖的是 Lua 的原子性，
    # 而 fakeredis 恰恰只在这里失败（见模块文档）。不测这一条会得出相反结论。
    "EVAL",
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
    """既没有 redis-server 二进制，也没有 fakeredis 也没有可连的外部 Redis。"""


def find_redis_binary(settings: Settings) -> Path | None:
    """按 ``配置目录 → 内置目录 → PATH`` 找 ``redis-server``。

    顺序的理由：显式配置永远最高；内置目录是本仓库/安装包自带的那份（可脱离系统
    环境独立运行，见 ``docs/desktop-local/``）；PATH 兜底开发者自己的 scoop/brew。
    """
    name = "redis-server.exe" if os.name == "nt" else "redis-server"
    if settings.redis_bin_dir:
        candidate = Path(settings.redis_bin_dir) / name
        if candidate.is_file():
            return candidate
    bundled = BUNDLED_REDIS_DIR / name
    if bundled.is_file():
        return bundled
    found = shutil.which(name)
    return Path(found) if found else None


class RedisServer:
    """Redis 客户端生命周期 + 命令级探测。"""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.client: Any = None
        self.backend = "none"
        self.error = ""
        self.warning = ""
        self.notes: list[str] = []
        self.lua_supported: bool | None = None
        self._pubsubs: list[Any] = []
        self._proc: Any = None
        self._port = 0

    # ------------------------------------------------------------------ 属性
    @property
    def port(self) -> int:
        """实际监听的 TCP 端口（未启动 / 外部 URL 后端为 0）。

        与 ``PostgresServer.port`` 对称：``redis_port`` 只是"建议值"，真正在监听哪一个
        由启动时挑的空闲端口决定。引导器要把这个值交给 Go 网关（``REDIS_ADDR``），
        所以必须暴露出来，而不是让调用方去读私有 ``_port``。
        """
        return self._port

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
            except Exception as exc:  # noqa: BLE001 —— 外部服务连不上就继续往下退
                self.warning = f"外部 Redis 不可用（{type(exc).__name__}: {exc}）；{VALKEY_HINT}"
                self.client = None

        # 内置二进制排在 fakeredis 之前：桩不支持 EVAL（见模块文档），
        # 而引擎的原子计数/仲裁全靠 Lua；宁可起一个真进程，也不要一个语义不全的桩。
        try:
            await self._start_bundled()
            return
        except Exception as exc:  # noqa: BLE001
            self.client = None
            self._proc = None
            self.notes.append(f"内置 redis 未用上（{type(exc).__name__}: {exc}）")

        try:
            self.client = self._build_fakeredis()
            await self.client.ping()
            self.backend = "fakeredis"
            self.error = ""
        except Exception as exc:  # noqa: BLE001
            self.client = None
            self.backend = "unavailable"
            detail = "；".join(self.notes)
            self.error = (
                f"既没有可用的内置 redis-server，也没有 fakeredis（{type(exc).__name__}: {exc}）"
                + (f"；{detail}" if detail else "")
                + f"；{VALKEY_HINT}"
            )

    async def _start_bundled(self) -> None:
        """拉起一个**本进程专属**的 ``redis-server``（随机端口 + 自己的数据目录）。

        为什么不用用户已有的 6379：那是别人的实例，往里写键会污染；而且
        ``requirepass``/版本都不可控。这里只借用二进制文件，进程与端口都是新的。
        """
        binary = find_redis_binary(self.settings)
        if binary is None:
            raise RedisUnavailable(
                "找不到 redis-server（可用 CHIRON_LOCAL_REDIS_BIN_DIR 指定目录，"
                "或把它放进 desktop-local/bin/redis/）"
            )

        port = int(self.settings.redis_port or 0) or find_free_port()
        data_dir = self.settings.redis_data_dir
        data_dir.mkdir(parents=True, exist_ok=True)
        self.settings.redis_log_file.parent.mkdir(parents=True, exist_ok=True)

        # 只绑 loopback；端口不写死；数据落在 data-dir 之下（重启后还在）。
        # 不开 AOF：本地模式是"缓存/协调"，重启后重建比追日志更简单，也少一堆文件。
        cmd = [
            str(binary),
            "--port",
            str(port),
            "--bind",
            "127.0.0.1",
            "--dir",
            str(data_dir),
            "--dbfilename",
            "dump.rdb",
            "--appendonly",
            "no",
            "--save",
            "",
        ]
        log = open(self.settings.redis_log_file, "ab")  # noqa: SIM115 —— 子进程持有，随进程退出关闭
        try:
            proc = subprocess.Popen(  # noqa: S603 —— 参数全由本进程拼，无用户输入
                cmd,
                stdout=log,
                stderr=subprocess.STDOUT,
                cwd=str(data_dir),
            )
        except FileNotFoundError as exc:
            log.close()
            raise RedisUnavailable(f"redis-server 无法执行（{binary}）：{exc}") from exc

        self._proc = proc
        self._port = port
        # wait_for_port 是阻塞轮询（最长 redis_start_timeout=30s）；bootstrap 跑在事件循环上，
        # 直接调用会让整个 core 在这段时间内无法响应 /healthz 与 /readyz。扔到线程里。
        listening = await asyncio.to_thread(
            wait_for_port, "127.0.0.1", port, self.settings.redis_start_timeout
        )
        if not listening:
            await self._stop_proc()
            raise RedisUnavailable(
                f"redis-server 在 {self.settings.redis_start_timeout:.0f}s 内没有监听 "
                f"127.0.0.1:{port}（见 {self.settings.redis_log_file}）"
            )

        import redis.asyncio as redis_asyncio

        self.client = redis_asyncio.from_url(
            f"redis://127.0.0.1:{port}/0", decode_responses=True
        )
        await self.client.ping()
        self.backend = "bundled"
        self.error = ""
        self.notes.append(f"内置 redis-server: {binary} @ 127.0.0.1:{port}")

    async def _stop_proc(self) -> None:
        """停掉自己拉起的 redis-server：先发 SHUTDOWN，再兜底 terminate。"""
        proc = self._proc
        self._proc = None
        if proc is None:
            return
        with contextlib.suppress(Exception):
            proc.terminate()
        with contextlib.suppress(Exception):
            proc.wait(timeout=5)
        with contextlib.suppress(Exception):
            proc.kill()

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

        # 自己拉起的实例：**先让 redis 自己退出**，再谈杀进程。
        # 为什么不能只 terminate()：安装包/scoop 提供的 redis-server.exe 往往只是个
        # wrapper（本机实测 D:\scoop\shims\redis-server.exe 就是），杀掉 wrapper 会把
        # 真正的 redis-server 留成孤儿进程 —— 端口继续监听（实测 stop 后 24637 仍在
        # LISTEN），下次启动就会撞端口，而且日志里看不出是谁占的。
        if client is not None and self._proc is not None:
            with contextlib.suppress(Exception):
                await client.shutdown(nosave=True)

        if client is not None:
            # 先让客户端退出，再停进程：顺序反了会留下"连着一个已死实例"的句柄
            try:
                await client.aclose()
            except Exception:  # noqa: BLE001
                with contextlib.suppress(Exception):
                    await client.close()
        await self._stop_proc()

    # ------------------------------------------------------------------ 健康
    async def health(self) -> dict[str, Any]:
        result: dict[str, Any] = {
            "ok": False,
            "backend": self.backend,
            "url": self.settings.redis_url,
            "error": self.error,
            "warning": self.warning,
            "lua": self.lua_supported,
        }
        if self.client is None:
            return result
        try:
            pong = await self.client.ping()
            result["ok"] = bool(pong)
        except Exception as exc:  # noqa: BLE001
            result["error"] = f"{type(exc).__name__}: {exc}"
        return result

    async def check_lua(self) -> ProbeResult:
        """跑一条最小的 Lua 脚本，确认这个后端**解释** Lua（而不只是认识命令名）。

        为什么单独测：EVAL 是本项目最关键的 Redis 能力（见模块文档）。``fakeredis``
        对 ``XADD``/``XREADGROUP`` 等七条命令全部 PASS，唯独 EVAL 直接报
        ``unknown command 'eval'`` —— 只看前七条会得出"fakeredis 够用"的错误结论。
        这里用 ``HINCRBY`` 而不是 ``GET``：原子自增是限流/预算/预留的真实用法。
        """
        self.lua_supported = None
        if self.client is None:
            return ProbeResult("EVAL", False, f"客户端未就绪：{self.error or '未启动'}")

        async def run() -> str:
            key = f"chiron:lua:{uuid.uuid4().hex[:8]}"
            script = (
                "redis.call('HSET', KEYS[1], 'n', 1) "
                "local n = redis.call('HINCRBY', KEYS[1], 'n', 2) "
                "redis.call('DEL', KEYS[1]) "
                "return n"
            )
            value = await self.client.eval(script, 1, key)
            return f"Lua 解释器可用（HINCRBY 1+2 → {value}）"

        result = await self._measure("EVAL", run)
        self.lua_supported = result.ok
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
            await self.check_lua(),
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
