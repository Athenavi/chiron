"""M52b：`llm:keys:changed` 订阅循环 —— 保存密钥后**当场**补注册，断线自己回来。

为什么必须有这条订阅（而不是"启动时扫一次"或"路由未命中时惰性刷"就够了）：引擎可能
**先于网关**就绪，网关把 PG 里的密钥镜像进 Redis keyset 时
引擎的 provider 表早就成型了。惰性刷新是兜底，它要先失败一次、还被去抖挡着，不能当主路径。

这里钉四件事：① 每条密钥变更消息都 **force** 刷新（绕过去抖，否则"点完保存立刻发消息"仍失败）；
② 订阅回执不是消息，不许触发刷新；③ 网关还没就绪时的消息直接丢弃，不许把订阅循环打死；
④ 连接断开/取消都要关掉 pubsub（否则每轮重连漏一条连接），并重新订阅。
"""

from __future__ import annotations

import ast
import asyncio
import pathlib
from typing import Any

from app import main as main_mod
from app.redis_keys import rkey

MAIN_PY = pathlib.Path(__file__).resolve().parents[1] / "app" / "main.py"


class _FakePubSub:
    def __init__(self, queue: asyncio.Queue) -> None:
        self._queue = queue
        self.subscribed: list[str] = []
        self.closed = 0
        self.delivered = 0

    async def subscribe(self, channel: str) -> None:
        self.subscribed.append(channel)

    async def listen(self):
        while True:
            item = await self._queue.get()
            if item is None:  # 模拟连接断开：生成器正常结束
                return
            self.delivered += 1
            yield item

    async def aclose(self) -> None:
        self.closed += 1


class _FakeRedis:
    def __init__(self) -> None:
        self.queue: asyncio.Queue = asyncio.Queue()
        self.pubsubs: list[_FakePubSub] = []

    def pubsub(self) -> _FakePubSub:
        ps = _FakePubSub(self.queue)
        self.pubsubs.append(ps)
        return ps


class _FakeGateway:
    def __init__(self) -> None:
        self.calls: list[tuple[bool, str]] = []

    async def refresh_providers(self, *, force: bool = False, reason: str = "") -> int:
        self.calls.append((force, reason))
        return 1

    async def close(self) -> None:
        """关闭段末尾会 `await _gateway.close()`；这里只需存在。"""


def _patch_redis(monkeypatch, redis: _FakeRedis) -> None:
    async def get_redis() -> _FakeRedis:
        return redis

    monkeypatch.setattr("app.redis_client.get_redis", get_redis)


async def _wait_until(predicate, what: str, timeout: float = 3.0) -> None:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while not predicate():
        if loop.time() > deadline:
            raise AssertionError(f"等不到：{what}")
        await asyncio.sleep(0.01)


async def _started_subscribed(monkeypatch) -> tuple[_FakeRedis, _FakeGateway, asyncio.Task]:
    redis = _FakeRedis()
    _patch_redis(monkeypatch, redis)
    gateway = _FakeGateway()
    monkeypatch.setattr(main_mod, "_gateway", gateway)
    task = main_mod._start_llm_keys_watch()
    await _wait_until(lambda: redis.pubsubs and redis.pubsubs[0].subscribed, "订阅没建立")
    return redis, gateway, task


async def _finish(task: asyncio.Task) -> None:
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass


async def test_broadcast_subscribes_the_prefixed_channel(monkeypatch) -> None:
    """订阅的是 `rkey()` 组装的键（多环境共用 Redis 时前缀不能少）。"""
    redis, _gateway, task = await _started_subscribed(monkeypatch)
    try:
        assert redis.pubsubs[0].subscribed == [rkey("llm:keys:changed")]
    finally:
        await _finish(task)


async def test_keyset_message_forces_a_provider_refresh(monkeypatch) -> None:
    """每条变更都 **force** 刷新，reason 里带上是哪个 provider。

    bytes 与 str 两种 payload 都要能读（redis-py 的 decode_responses 取决于配置）。
    订阅回执（type=subscribe）**不是**消息，不该触发刷新。
    """
    redis, gateway, task = await _started_subscribed(monkeypatch)
    try:
        await redis.queue.put({"type": "subscribe", "data": b"llm:keys:changed"})
        await redis.queue.put({"type": "message", "data": b"moonshot"})
        await redis.queue.put({"type": "message", "data": "openai"})
        await _wait_until(lambda: len(gateway.calls) == 2, "两次密钥变更没各触发一次刷新")
    finally:
        await _finish(task)

    assert gateway.calls == [(True, "keyset-changed:moonshot"), (True, "keyset-changed:openai")], (
        "force 丢了 = 保存密钥后立刻发的第一条消息还会失败"
    )


async def test_message_before_gateway_ready_is_dropped_not_fatal(monkeypatch) -> None:
    """网关还没就绪（`_gateway` 为 None）时收到消息：丢掉这一条，订阅循环不许死。

    早退成异常会被外层 except 吞掉并退避 5 秒 —— 那条连接当场就被关掉（泄漏口径变了），
    而退避期间到达的消息没人处理，表现正好是「配了 key 但有时生效有时不生效」。
    """
    redis = _FakeRedis()
    _patch_redis(monkeypatch, redis)
    monkeypatch.setattr(main_mod, "_gateway", None)
    task = main_mod._start_llm_keys_watch()
    try:
        await _wait_until(lambda: redis.pubsubs and redis.pubsubs[0].subscribed, "订阅没建立")
        for _ in range(3):
            await redis.queue.put({"type": "message", "data": b"deepseek"})
        await _wait_until(lambda: redis.pubsubs[0].delivered == 3, "未就绪的消息没被消费掉")
        assert redis.pubsubs[0].closed == 0, "把「网关未就绪」当成断线：连接被丢掉并开始退避"

        gateway = _FakeGateway()
        monkeypatch.setattr(main_mod, "_gateway", gateway)
        await redis.queue.put({"type": "message", "data": b"openai"})
        await _wait_until(lambda: bool(gateway.calls), "就绪后的消息没被处理（还卡在退避里）")
    finally:
        await _finish(task)

    assert gateway.calls == [(True, "keyset-changed:openai")]


async def test_connection_loss_reconnects_and_closes_the_old_pubsub(monkeypatch) -> None:
    """连接断开 ⇒ 关掉旧 pubsub、退避后重新订阅（每条连接都要有归宿，否则泄漏）。"""
    redis, gateway, task = await _started_subscribed(monkeypatch)
    try:
        await redis.queue.put(None)  # 断开
        await _wait_until(lambda: len(redis.pubsubs) == 2, "断线后没有重新订阅")
        await redis.queue.put({"type": "message", "data": b"moonshot"})
        await _wait_until(lambda: bool(gateway.calls), "重连后消息没人处理")
    finally:
        await _finish(task)

    assert redis.pubsubs[0].closed == 1, "旧 pubsub 没关：每断一次线漏一条连接"
    assert redis.pubsubs[1].closed == 1, "取消时没走 finally：连接留到事件循环销毁之后"


async def test_shutdown_cancels_the_keyset_listener(monkeypatch) -> None:
    """关闭段必须取消它（句柄**显式传入**）：不取消就卡在 `queue.get()` 上，退出拖住事件循环。"""
    redis, _gateway, task = await _started_subscribed(monkeypatch)
    assert isinstance(task, asyncio.Task), "_start_llm_keys_watch 必须把句柄交给调用方"

    await main_mod._shutdown_lifespan(llm_keys_task=task)

    assert task.cancelled() or task.done()
    assert redis.pubsubs[0].closed == 1


def _fn(name: str) -> ast.AST:
    tree = ast.parse(MAIN_PY.read_text(encoding="utf-8"))
    return next(
        node
        for node in ast.walk(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name
    )


def test_listener_refreshes_with_force_true_at_the_call_site() -> None:
    """机械护栏：`refresh_providers` 在订阅循环里**必须**写 `force=True`。

    这个参数是关键字参数、默认 False，漏写不会报错，只会静默退回"被去抖挡住"——
    而运行时的表现（第一条消息失败）和用例里的表现差一个时间窗口，最难查。
    """
    calls = [
        node
        for node in ast.walk(_fn("_listen_llm_keys_changes"))
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "refresh_providers"
    ]
    assert calls, "订阅循环不再调用 refresh_providers 了：广播失效链路断了"
    for call in calls:
        force = next((kw for kw in call.keywords if kw.arg == "force"), None)
        assert force is not None and isinstance(force.value, ast.Constant) and force.value.value is True, (
            "refresh_providers 缺 force=True —— 保存密钥后立刻发的第一条消息会被去抖挡掉"
        )


def test_gateway_start_hands_the_listener_handle_to_lifespan() -> None:
    """机械护栏：句柄靠 `_start_gateway` **返回**、lifespan 传进关闭段。

    用模块全局也能跑，但那就绕开了本文件钉住的"句柄显式传递"契约（`locals()` 探测当年
    就是在这里静默失效的），而且 `test_lifespan_passes_every_shutdown_parameter` 看不见它。
    """
    returns: list[Any] = [
        node
        for node in ast.walk(_fn("_start_gateway"))
        if isinstance(node, ast.Return)
        and isinstance(node.value, ast.Call)
        and isinstance(node.value.func, ast.Name)
        and node.value.func.id == "_start_llm_keys_watch"
    ]
    assert returns, "_start_gateway 没把密钥订阅任务返回出去 —— 关闭段拿不到它，订阅永不取消"

    lifespan = _fn("lifespan")
    assigned = {
        target.id
        for node in ast.walk(lifespan)
        if isinstance(node, ast.Assign)
        and isinstance(node.value, ast.Await)
        and isinstance(node.value.value, ast.Call)
        and isinstance(node.value.value.func, ast.Name)
        and node.value.value.func.id == "_start_gateway"
        for target in node.targets
        if isinstance(target, ast.Name)
    }
    assert assigned, "lifespan 没接住 _start_gateway() 的返回值 —— 句柄在启动段就丢了"
