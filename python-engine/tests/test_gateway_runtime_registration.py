"""M52b：provider 注册表在**运行期**可增长 —— 配完 key 不必重启引擎。

真机现象：/chat 发消息永远停在「思考中」。根因是注册表在 lifespan 里
一次成型，而引擎可能**先于网关**就绪：网关把 PG 里的密钥镜像进
Redis keyset 时，引擎的 `_providers` 已经冻结成空表，于是每条消息都走
`no LLM provider available` —— 旧文案还让人去查 `.env`，而打包态根本没有 `.env`。

这里钉四件事：
① `register_provider` 是三张运行期表（chat 熔断 / embed 熔断 / 延迟）+ 目录的**唯一**入口，
   漏任何一张，第一次调用就 KeyError（表现是「配了 key 反而更糟」）；
② 路由未命中、全部熔断两种失败前都会补注册一次，且递归被去抖挡住（不会自选死循环）；
③ 去抖只挡惰性刷新，`force=True`（密钥变更广播）必真刷；
④ 报错文案按「表空」与「表非空但选不出」两种真不同的故障分开指路。
"""

from __future__ import annotations

import asyncio

from app.gateway.provider import ChatResponse, EmbeddingResponse, LLMProvider
from app.gateway.router import GatewayRouter


class _Provider(LLMProvider):
    """最小可用 provider：实例级 name，便于同一路由表里塞多个。"""

    def __init__(self, name: str) -> None:
        self.name = name

    async def chat_stream(self, messages, model, **kwargs):
        yield ChatResponse(content=f"{self.name}:ok", finish_reason="stop")

    async def chat(self, messages, model, **kwargs):
        return ChatResponse(content=f"{self.name}:ok", finish_reason="stop")

    async def embed(self, text, model):
        return EmbeddingResponse()

    async def close(self):
        pass


def _preset(provider_id: str, *prefixes: str) -> dict[str, object]:
    return {"id": provider_id, "model_prefixes": list(prefixes), "cost": 0.4, "quality": 0.8}


def _trip(breaker) -> None:
    for _ in range(breaker.failure_threshold):
        breaker.record_failure()
    assert not breaker.allow(), "熔断没打开，用例前提不成立"


async def _last_chunk(router: GatewayRouter, model: str) -> ChatResponse:
    chunks = [c async for c in router.chat_stream([], model, tenant_id="t1")]
    return chunks[-1]


async def test_register_provider_seeds_every_runtime_table() -> None:
    """运行期注册的 provider 必须同时进 chat 熔断 / embed 熔断 / 延迟 / 目录四张表。

    这三张表在构造时按**当时的** provider 名单生成，`_select` 用的是 `breakers[p.name]`
    直接下标：只加 `_providers` 不加表，第一次路由就 KeyError。
    `_catalog` 同样是构造时从目录列表拷出来的 dict —— 运行期往那个列表 append 传不进来，
    所以 preset 必须由 `register_provider` 显式带上，否则按前缀路由匹配不到。
    """
    router = GatewayRouter(providers={})
    router.register_provider("moonshot", _Provider("moonshot"), _preset("moonshot", "kimi"))

    assert (await router._select("kimi-k2")) is not None
    assert router._breakers["moonshot"].allow()
    assert router._embed_breakers["moonshot"].allow()
    assert router._latencies["moonshot"] == 500.0


async def test_runtime_registered_provider_serves_a_stream() -> None:
    """端到端一点：运行期注册的 provider 真能接住一条消息（走的是 report/latency 路径）。"""
    router = GatewayRouter(providers={})
    router.register_provider("moonshot", _Provider("moonshot"), _preset("moonshot", "kimi"))

    chunk = await _last_chunk(router, "kimi-k2")

    assert chunk.finish_reason == "stop"
    assert chunk.content == "moonshot:ok"


async def test_runtime_registered_provider_is_routed_by_prefix() -> None:
    """preset 必须进 `_catalog`：否则按 `provider/model` 前缀匹配不到，请求会被兜底打到别的 provider。

    兜底规则里 `openai` 优先（最通用），所以"目录没进来"不是报错而是**悄悄选错**——
    用户配的是 moonshot 的 kimi，实际发出去的是 openai。
    """
    router = GatewayRouter(providers={"openai": _Provider("openai")})
    router.register_provider("moonshot", _Provider("moonshot"), _preset("moonshot", "kimi"))

    assert (await router._select("kimi-k2")).name == "moonshot"


async def test_runtime_registered_provider_can_serve_embed() -> None:
    """embed 用**独立**的熔断表，也要在运行期注册时补上（漏了就是第一次 embed 就 KeyError）。"""
    router = GatewayRouter(providers={})
    router.register_provider("moonshot", _Provider("moonshot"), _preset("moonshot", "kimi"))

    await router.embed("你好", "kimi-embed")  # 不抛异常即为通过（embed 失败会返回空向量）

    assert router._embed_breakers["moonshot"].allow()


async def test_register_provider_keeps_an_existing_breaker_state() -> None:
    """重复注册不许 reset 熔断：刚被记了失败的 provider 不该因为"补注册"满血复活。"""
    router = GatewayRouter(providers={"moonshot": _Provider("moonshot")})
    _trip(router._breakers["moonshot"])

    router.register_provider("moonshot", _Provider("moonshot"), _preset("moonshot", "kimi"))

    assert not router._breakers["moonshot"].allow()


async def test_route_miss_registers_the_late_key_provider() -> None:
    """一个都匹配不到时先补注册再选 —— 这正是「先起引擎、后配 key」的桌面态。

    只断言"最后选到了 provider"分不清是哪条刷新路径救的：把 route-miss 那条删掉，
    空表立刻撞上下一分支 all-circuit-open，照样能把 provider 补进来（变异体因此活了下来）。
    所以这里钉**顺序**：第一条触发必须是 route-miss，且命中后不再刷第二轮。
    """
    seen: list[tuple[str, bool]] = []

    async def refresh() -> int:
        router.register_provider("moonshot", _Provider("moonshot"), _preset("moonshot", "kimi"))
        return 1

    router = GatewayRouter(providers={}, provider_refresh=refresh)
    refresh_providers = router.refresh_providers

    async def spy(*, force: bool = False, reason: str = "") -> int:
        seen.append((reason, force))
        return await refresh_providers(force=force, reason=reason)

    router.refresh_providers = spy  # type: ignore[method-assign]

    provider = await router._select("kimi-k2")

    assert provider is not None and provider.name == "moonshot"
    assert seen and seen[0] == ("route-miss", False), seen
    assert len(seen) == 1, "补注册后重选命中就不该再刷第二轮"


async def test_all_circuit_open_still_gets_a_fresh_provider() -> None:
    """全熔断也可能是「新 provider 还没进表」：老 provider 正好都坏了，补注册后再看一次。"""
    old = _Provider("openai")
    router = GatewayRouter(providers={"openai": old})
    _trip(router._breakers["openai"])

    async def refresh() -> int:
        router.register_provider("moonshot", _Provider("moonshot"), _preset("moonshot", "kimi"))
        return 1

    router._provider_refresh = refresh

    provider = await router._select("kimi-k2")

    assert provider is not None and provider.name == "moonshot"


async def test_refresh_debounced_but_force_bypasses() -> None:
    """去抖：路由未命中每个请求都会触发一次刷新，不去抖就每轮扫 Redis。

    `force=True` 必须绕过它 —— 管理端点保存后用户立刻发消息，两次变更常常挤在同一个
    去抖窗口里，按去抖处理就成了「我明明配了 key，第一条还是失败」。
    """
    calls: list[str] = []

    async def refresh() -> int:
        calls.append("x")
        return 0

    router = GatewayRouter(providers={}, provider_refresh=refresh)

    assert await router.refresh_providers() == 0
    assert await router.refresh_providers() == 0
    assert len(calls) == 1, "第二次惰性刷新没被去抖 —— 每个失败请求都会扫一遍 keyset"

    assert await router.refresh_providers(force=True) == 0
    assert len(calls) == 2, "force 刷新被去抖挡掉了 —— 保存密钥后当场不生效"


async def test_concurrent_refresh_never_overlaps_the_scan() -> None:
    """单飞：两次补注册不许同时在扫目录 + keyset。

    去抖只挡"在后面来的那一次"，挡不住"同时来的那一次"；而 `force=True`（密钥变更广播）
    本来就要绕过抖动，连着保存多个 provider 的 key 会挤出一串并发刷新。并发跑不只是浪费
    一次 Redis 扫描，还会交错改 `_providers`。把 `async with self._refresh_lock` 删掉，
    这条立刻变成峰值 2（变异验证已确认）。
    """
    entered = asyncio.Event()
    release = asyncio.Event()
    in_flight = 0
    peak = 0

    async def refresh() -> int:
        nonlocal in_flight, peak
        in_flight += 1
        peak = max(peak, in_flight)
        entered.set()
        await release.wait()
        in_flight -= 1
        return 0

    router = GatewayRouter(providers={}, provider_refresh=refresh)
    first = asyncio.create_task(router.refresh_providers(force=True))
    await asyncio.wait_for(entered.wait(), timeout=3)
    second = asyncio.create_task(router.refresh_providers(force=True))
    await asyncio.sleep(0)
    await asyncio.sleep(0)

    release.set()
    await asyncio.wait_for(asyncio.gather(first, second), timeout=3)

    assert peak == 1, f"两次补注册重叠了（峰值 {peak}）—— _refresh_lock 没把它们串起来"


async def test_refresh_callback_failure_is_not_fatal() -> None:
    """补注册自己挂了（Redis 不通）不许把这一轮请求变成异常，也不许卡死选路。"""

    async def boom() -> int:
        raise RuntimeError("redis down")

    router = GatewayRouter(providers={}, provider_refresh=boom)

    assert await router._select("kimi-k2") is None

    chunk = await _last_chunk(router, "kimi-k2")
    assert chunk.finish_reason == "error"


async def test_empty_registry_message_points_at_admin_console() -> None:
    """表是空的 ⇒ 没有任何 provider 被注册：指向「管理后台 → API 密钥」，并说明不必重启。

    旧文案 `check API keys in .env` 在打包桌面态里把人引向一个不存在的东西。
    """
    router = GatewayRouter(providers={})

    chunk = await _last_chunk(router, "kimi-k2")

    message = chunk.message or ""
    assert chunk.finish_reason == "error"
    assert "管理后台" in message and "API 密钥" in message
    assert "无需重启" in message
    assert ".env" not in message, "桌面态没有用户可见的 .env，这条提示只会引偏"


async def test_nonempty_registry_message_names_the_registered_providers() -> None:
    """表非空却选不出来 ⇒ provider 都在，是端点/密钥/熔断问题，别再让人去配 key。"""
    router = GatewayRouter(providers={"openai": _Provider("openai")})
    _trip(router._breakers["openai"])

    chunk = await _last_chunk(router, "kimi-k2")

    message = chunk.message or ""
    assert chunk.finish_reason == "error"
    assert "openai" in message, "没告诉用户哪些 provider 已经在了"
    assert "熔断" in message
