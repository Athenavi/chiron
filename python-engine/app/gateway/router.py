# Gateway 路由器 — 多 Provider 加权选择 + 熔断 + 降级
from __future__ import annotations

import logging
import random
import sys
import time
from typing import AsyncIterator, Optional

import aiohttp

from app.gateway.budget import TokenBudget
from app.gateway.cache import SemanticCache
from app.gateway.circuit_breaker import CircuitBreaker
from app.gateway.provider import (ChatMessage, ChatResponse, EmbeddingResponse,
                                  LLMProvider)

logger = logging.getLogger(__name__)


def normalize_messages(messages: list) -> list[ChatMessage]:
    """将裸 dict 消息统一规范化为 ChatMessage。

    历史上多处调用方（task_router/skill 工具/workflow 引擎等）直接传
    ``[{"role": ..., "content": ...}]``，而缓存层 ``_exact_key`` 调用
    ``m.to_dict()``、语义层访问 ``m.role/m.content`` — dict 会在入口处
    抛 AttributeError。网关入口统一转换，调用方无需感知。
    （冒烟测试 2026-08-21 实测踩坑后引入）
    """
    normalized: list[ChatMessage] = []
    for m in messages:
        if isinstance(m, ChatMessage):
            normalized.append(m)
        elif isinstance(m, dict):
            normalized.append(
                ChatMessage(
                    role=str(m.get("role", "user")),
                    content=m.get("content") or "",
                    tool_call_id=str(m.get("tool_call_id", "") or ""),
                )
            )
        else:
            raise TypeError(f"unsupported message type: {type(m).__name__}")
    return normalized


class GatewayRouter:
    """
    核心路由器 — 所有 LLM 调用的统一入口。

    路由决策:
      1. provider_hint 指定 → 直接用（如果未熔断）
      2. 找到支持 model 的所有 providers
      3. 过滤已熔断的
      4. 加权随机选择: score = w_cost * (1/price) + w_latency * (1/latency) + w_quality
      5. 全部熔断 → 尝试 fallback provider
      6. 租户路由配置（从 Go 网关同步）覆盖默认选择

    缓存: chat 请求自动走 SemanticCache
    预算: chat 请求自动扣减 TokenBudget
    限流: 调用方在 middleware 层做，此处不重复
    """

    # Provider 默认定价 ($/1M tokens)，用于加权路由
    DEFAULT_COST: dict[str, float] = {
        "anthropic": 3.0,
        "openai": 5.0,
        "deepseek": 0.2,
    }
    DEFAULT_QUALITY: dict[str, float] = {
        "anthropic": 0.95,
        "openai": 0.90,
        "deepseek": 0.80,
    }

    # 探活模型兜底表（provider → 廉价模型名）。目录项的 probe_model 优先，
    # 未列出的 provider 不做探活（避免用错模型名触发 404 误开熔断）。
    PROBE_MODELS: dict[str, str] = {
        "openai": "gpt-3.5-turbo",
        "anthropic": "claude-3-haiku-20240307",
        "deepseek": "deepseek-chat",
        "google": "gemini-2.0-flash",
        "xai": "grok-beta",
        "groq": "llama-3.1-8b-instant",
        "mistral": "mistral-small-latest",
        "moonshot": "moonshot-v1-8k",
        "zhipu": "glm-4-flash",
        "dashscope": "qwen-turbo",
        "minimax": "abab6.5s-chat",
        "baichuan": "Baichuan4",
        "hunyuan": "hunyuan-lite",
        "stepfun": "step-1-8k",
        "openrouter": "openai/gpt-4o-mini",
        "siliconflow": "Qwen/Qwen2.5-7B-Instruct",
        "ollama": "llama3.2",
    }

    def __init__(
        self,
        providers: dict[str, LLMProvider],
        cache: SemanticCache | None = None,
        budget: TokenBudget | None = None,
        weights: dict[str, float] | None = None,
        gateway_url: str = "",
        internal_token: str = "",
        provider_catalog: list[dict] | None = None,
    ):
        self._providers = providers
        # 服务提供商目录（app/providers/catalog.py，权威源在 Go 网关）：驱动
        # 「模型名 → provider」匹配与加权路由的成本/质量分；为空时回落内建默认。
        self._catalog: dict[str, dict] = {
            str(item.get("id")): item
            for item in (provider_catalog or [])
            if isinstance(item, dict) and item.get("id")
        }
        self._breakers = {name: CircuitBreaker() for name in providers}
        # embed 用**独立**的熔断表：embeddings 打不通不该让聊天也不可用。
        # 真实故障：opencode-go 的 base_url 下没有 /embeddings（返回 HTML 404），
        # 该失败连续累积把**共用**的熔断器打到 15 次，于是连 chat_stream 都进不去 ——
        # 用户看到的却是 "no LLM provider available for model=…"，把方向引到了模型上。
        self._embed_breakers = {name: CircuitBreaker() for name in providers}
        self._latencies: dict[str, float] = {
            name: 500.0 for name in providers
        }  # moving avg ms
        self._cache = cache
        self._budget = budget
        # 路由权重: cost / latency / quality
        self._weights = weights or {"cost": 0.3, "latency": 0.3, "quality": 0.4}
        # 租户路由配置（从 Go 网关同步）
        self._tenant_routes: dict[str, list[dict]] = {}  # tenant_id -> [route, ...]
        self._gateway_url = gateway_url
        self._internal_token = internal_token

    # ── 公开 API ──

    async def chat_stream(
        self,
        messages: list[ChatMessage],
        model: str,
        *,
        tenant_id: str = "",
        provider_hint: str = "",
        max_tokens: int = 4096,
        temperature: float = 0.7,
        tools: list[dict] | None = None,
    ) -> AsyncIterator[ChatResponse]:
        """流式推理 — 前置缓存检查 + 预算扣减"""
        messages = normalize_messages(messages)
        # ── 缓存查找（仅无工具时） ──
        if self._cache and not tools:
            cached = await self._cache.lookup(tenant_id, model, messages, tools, temperature)
            if cached:
                logger.info("Stream cache HIT for model=%s", model)
                yield cached
                return

        provider = await self._select(model, provider_hint, tenant_id=tenant_id)
        if not provider:
            yield ChatResponse(
                content="",
                finish_reason="error",
                provider="none",
                model=model,
                message="no LLM provider available for model="
                + model
                + " (check API keys in .env; configured: "
                + ",".join(self._providers.keys())
                + ")",
            )
            return

        # 预算预检查
        estimated = max_tokens  # 粗估
        if self._budget and tenant_id:
            ok = await self._budget.check(tenant_id, estimated)
            if not ok:
                yield ChatResponse(
                    content="",
                    finish_reason="error",
                    provider=provider.name,
                    model=model,
                    message="token budget exceeded for tenant " + tenant_id,
                )
                return

        start = time.monotonic()
        full_content = ""
        full_tool_calls = []
        finish_reason = ""
        total_input = 0
        total_output = 0
        try:
            async for chunk in provider.chat_stream(
                messages,
                model,
                max_tokens=max_tokens,
                temperature=temperature,
                tools=tools,
            ):
                chunk.provider = provider.name
                chunk.model = model
                # 累积用于缓存
                if chunk.content:
                    full_content += chunk.content
                if chunk.tool_calls:
                    full_tool_calls.extend(chunk.tool_calls)
                if chunk.finish_reason:
                    finish_reason = chunk.finish_reason
                if chunk.input_tokens:
                    total_input += chunk.input_tokens
                if chunk.output_tokens:
                    total_output += chunk.output_tokens
                yield chunk

            self._breakers[provider.name].record_success()

            # ── 流完成后存入缓存（仅无工具时） ──
            if self._cache and not tools and finish_reason != "error" and full_content:
                resp = ChatResponse(
                    content=full_content,
                    finish_reason=finish_reason,
                    provider=provider.name,
                    model=model,
                    input_tokens=total_input,
                    output_tokens=total_output,
                )
                await self._cache.store(tenant_id, model, messages, tools, temperature, resp)

            # 缓存正常路径仅存结果；预算扣减统一在 finally 处理（见下）

        except Exception as e:
            self._breakers[provider.name].record_failure()
            logger.error("Provider %s chat_stream failed: %s", provider.name, e)
            # 不要将原始异常文本当作 assistant 回复内容发送（会显示成
            # "Connection error." 之类的假回复）；发 error chunk，
            # 由上层 AgentRuntime 转成 error 事件（message 携带真实原因）。
            yield ChatResponse(
                content="",
                finish_reason="error",
                provider=provider.name,
                model=model,
                message=f"provider {provider.name} stream failed: {type(e).__name__}: {e}",
            )
        finally:
            # S 修复：预算扣减放入 finally——正常/异常路径都会按实际用量扣除。
            # GeneratorExit(客户端断开)取消时 await 不安全，跳过(少量少计可接受)。
            _exc = sys.exc_info()[1]
            if (
                not isinstance(_exc, GeneratorExit)
                and self._budget
                and tenant_id
                and (total_input + total_output) > 0
            ):
                try:
                    await self._budget.deduct(tenant_id, total_input + total_output)
                except Exception:
                    logger.warning("budget deduct failed (non-blocking)")
            elapsed = (time.monotonic() - start) * 1000
            self._latencies[provider.name] = (
                self._latencies[provider.name] * 0.8 + elapsed * 0.2
            )

    async def chat(
        self,
        messages: list[ChatMessage],
        model: str,
        *,
        tenant_id: str = "",
        provider_hint: str = "",
        max_tokens: int = 4096,
        temperature: float = 0.7,
        tools: list[dict] | None = None,
    ) -> ChatResponse:
        """非流式推理 — 自动缓存 + 预算扣减"""
        messages = normalize_messages(messages)
        # 缓存查找
        if self._cache and not tools:
            cached = await self._cache.lookup(tenant_id, model, messages, tools, temperature)
            if cached:
                logger.info("Cache hit for model=%s", model)
                return cached

        provider = await self._select(model, provider_hint, tenant_id=tenant_id)
        if not provider:
            return ChatResponse(
                content="",
                finish_reason="error",
                provider="none",
                model=model,
            )

        # 预算预检查
        if self._budget and tenant_id:
            ok = await self._budget.check(tenant_id, max_tokens)
            if not ok:
                return ChatResponse(
                    content="",
                    finish_reason="error",
                    provider=provider.name,
                    model=model,
                )

        start = time.monotonic()
        try:
            resp = await provider.chat(
                messages,
                model,
                max_tokens=max_tokens,
                temperature=temperature,
                tools=tools,
            )
            resp.provider = provider.name
            resp.model = model

            self._breakers[provider.name].record_success()

            # 存缓存
            if self._cache and not tools and resp.finish_reason != "error":
                await self._cache.store(tenant_id, model, messages, tools, temperature, resp)

            # 预算扣减
            if (
                self._budget
                and tenant_id
                and resp.input_tokens + resp.output_tokens > 0
            ):
                await self._budget.deduct(
                    tenant_id, resp.input_tokens + resp.output_tokens
                )

            return resp
        except Exception as e:
            self._breakers[provider.name].record_failure()
            logger.error("Provider %s chat failed: %s", provider.name, e)
            return ChatResponse(
                content="",
                finish_reason="error",
                provider=provider.name,
                model=model,
            )
        finally:
            elapsed = (time.monotonic() - start) * 1000
            self._latencies[provider.name] = (
                self._latencies[provider.name] * 0.8 + elapsed * 0.2
            )

    async def embed(
        self, text: str, model: str, provider_hint: str = ""
    ) -> EmbeddingResponse:
        """文本嵌入 — 无缓存无预算。

        用**独立**的熔断表（embed=True）：某些网关（如 opencode-go）根本不提供
        /embeddings，那里失败不该把聊天也一起熔断。
        """
        provider = await self._select(model, provider_hint, embed=True)
        if not provider:
            return EmbeddingResponse()

        try:
            resp = await provider.embed(text, model)
            self._embed_breakers[provider.name].record_success()
            return resp
        except Exception as e:
            self._embed_breakers[provider.name].record_failure()
            logger.error("Provider %s embed failed: %s", provider.name, e)
            return EmbeddingResponse()

    def _probe_model(self, name: str) -> str:
        """探活用的廉价模型：目录 probe_model 优先，其次内建兜底表；空 = 不探活。"""
        preset = self._catalog.get(name) or {}
        model = str(preset.get("probe_model") or "").strip()
        if model:
            return model
        return self.PROBE_MODELS.get(name, "")

    async def health_check(self) -> dict:
        """返回各 Provider 健康状态"""
        result = {}
        for name, provider in self._providers.items():
            state = self._breakers[name].state.value
            probe_model = self._probe_model(name)
            if not probe_model:
                # 目录未给探活模型（自定义端点等）：不探活，避免用错模型名
                # 触发 404 → 误开熔断；状态交由真实调用反馈。
                result[name] = {"status": "ok", "circuit": state, "probe": "skipped"}
                continue
            try:
                # 尝试廉价模型调用 probe
                resp = await provider.chat(
                    [ChatMessage(role="user", content="ping")],
                    model=probe_model,
                    max_tokens=1,
                    temperature=0,
                )
                if resp.finish_reason != "error":
                    self._breakers[name].record_success()
                    result[name] = {"status": "ok", "circuit": state}
                else:
                    result[name] = {"status": "error", "circuit": state, "message": "probe failed"}
            except Exception as e:
                self._breakers[name].record_failure()
                result[name] = {"status": "error", "circuit": state, "message": str(e)}
        return result

    async def reset_breaker(self, provider_name: str) -> bool:
        """重置指定 provider 的熔断器为 closed 状态。

        返回 True 表示成功，False 表示 provider 不存在。
        用于管理端手动恢复熔断的 provider。
        """
        if provider_name not in self._breakers:
            return False
        self._breakers[provider_name] = CircuitBreaker()
        logger.info("Circuit breaker reset for provider=%s", provider_name)
        return True

    async def close(self) -> None:
        for p in self._providers.values():
            await p.close()

    def stats(self) -> dict:
        """返回路由器统计"""
        return {
            "providers": {
                name: {
                    "circuit_state": self._breakers[name].state.value,
                    "avg_latency_ms": round(self._latencies[name], 1),
                }
                for name in self._providers
            },
            "cache": self._cache.stats() if self._cache else None,
            "tenant_routes": len(self._tenant_routes),
        }

    # ── 租户路由同步 ──

    async def sync_routes(self) -> int:
        """从 Go 网关同步租户模型路由配置，启动时调用。

        返回同步的路由条目数，失败返回 0（不阻断启动）。

        此方法也暴露给 POST /v1/gateway/sync-routes 端点，支持运行时热切换。
        """
        if not self._gateway_url or not self._internal_token:
            logger.info("sync_routes skipped: gateway_url or internal_token not set")
            return 0
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(
                    f"{self._gateway_url}/v1/internal/model-routes",
                    headers={"X-Internal-Token": self._internal_token},
                    timeout=aiohttp.ClientTimeout(total=10),
                ) as resp:
                    if resp.status != 200:
                        logger.warning("sync_routes HTTP %d from gateway", resp.status)
                        return 0
                    data = await resp.json()
                    routes = data.get("routes", [])
        except Exception as e:
            logger.warning("sync_routes failed: %s", e)
            return 0

        # 按 tenant_id 分组
        tenant_map: dict[str, list[dict]] = {}
        for r in routes:
            tid = r.get("tenant_id", "")
            if not tid:
                continue
            if tid not in tenant_map:
                tenant_map[tid] = []
            tenant_map[tid].append(r)
        self._tenant_routes = tenant_map
        logger.info("sync_routes loaded %d routes for %d tenants", len(routes), len(tenant_map))
        return len(routes)

    def _get_tenant_provider(self, model: str, tenant_id: str) -> Optional[str]:
        """按租户路由配置获取首选 provider 名称。

        返回 None 表示无匹配路由（回退默认加权选择）。
        """
        if not tenant_id or tenant_id not in self._tenant_routes:
            return None
        for route in self._tenant_routes.get(tenant_id, []):
            if route.get("model_id") == model and route.get("enabled", True):
                return route.get("primary_provider")
        return None

    # ── 内部路由 ──

    async def _select(
        self, model: str, hint: str = "", tenant_id: str = "", embed: bool = False
    ) -> Optional[LLMProvider]:
        """选择 Provider — 支持租户路由覆盖。

        embed=True 时使用**独立**的熔断表：embed 与 chat 的可用性互不牵连
        （见 __init__ 里 _embed_breakers 的注释）。
        """
        breakers = self._embed_breakers if embed else self._breakers
        # 租户路由优先
        if tenant_id:
            tenant_provider = self._get_tenant_provider(model, tenant_id)
            if tenant_provider and tenant_provider in self._providers:
                if breakers[tenant_provider].allow():
                    return self._providers[tenant_provider]
                logger.warning(
                    "Tenant route provider %s circuit-open for tenant=%s model=%s, falling back",
                    tenant_provider, tenant_id, model,
                )
        if hint and hint in self._providers:
            if breakers[hint].allow():
                return self._providers[hint]
            logger.warning("Hinted provider %s is circuit-open, falling back", hint)

        # 找到所有支持该 model 的 provider
        candidates = self._find_candidates(model)
        if not candidates:
            # 全部尝试
            candidates = list(self._providers.values())

        # 过滤熔断
        available = [p for p in candidates if breakers[p.name].allow()]

        if not available:
            logger.error(
                "All providers circuit-open for %s model=%s",
                "embed" if embed else "chat",
                model,
            )
            return None

        # 加权选择
        return self._weighted_select(available)

    def _find_candidates(self, model: str) -> list[LLMProvider]:
        """按 model 匹配候选 provider。

        优先用服务提供商目录的 ``model_prefixes``（新增 provider 只需补目录）；
        目录未命中或无目录时保留内建前缀规则，行为与旧版一致。
        """
        model_lower = model.lower()
        matched: list[LLMProvider] = []
        for name, preset in self._catalog.items():
            if name not in self._providers:
                continue
            prefixes = preset.get("model_prefixes") or []
            if any(
                str(prefix).lower() in model_lower
                for prefix in prefixes
                if str(prefix).strip()
            ):
                matched.append(self._providers[name])
        if matched:
            return matched

        if "claude" in model_lower and "anthropic" in self._providers:
            return [self._providers["anthropic"]]
        if "deepseek" in model_lower and "deepseek" in self._providers:
            return [self._providers["deepseek"]]
        # gpt / o1 / o3 / embedding → openai
        if (
            "gpt" in model_lower
            or "o1" in model_lower
            or "o3" in model_lower
            or "embedding" in model_lower
        ) and "openai" in self._providers:
            return [self._providers["openai"]] if "openai" in self._providers else []
        # 兜底: 优先用 openai（最通用）
        if "openai" in self._providers:
            return [self._providers["openai"]]
        return list(self._providers.values())

    def _provider_cost(self, name: str) -> float:
        """provider 参考成本（$/1M tokens）：目录优先，其次内建默认。"""
        preset = self._catalog.get(name) or {}
        cost = preset.get("cost")
        if isinstance(cost, (int, float)):
            return float(cost)
        return self.DEFAULT_COST.get(name, 5.0)

    def _provider_quality(self, name: str) -> float:
        """provider 参考质量分（0-1）：目录优先，其次内建默认。"""
        preset = self._catalog.get(name) or {}
        quality = preset.get("quality")
        if isinstance(quality, (int, float)):
            return float(quality)
        return self.DEFAULT_QUALITY.get(name, 0.5)

    def _weighted_select(self, providers: list[LLMProvider]) -> LLMProvider:
        """加权随机选择"""
        if len(providers) == 1:
            return providers[0]

        scores = []
        for p in providers:
            # 免费/自托管 provider 成本为 0，取下限避免除零放大权重
            cost = max(self._provider_cost(p.name), 0.01)
            latency = max(self._latencies.get(p.name, 500.0), 1.0)
            quality = self._provider_quality(p.name)

            score = (
                self._weights["cost"] * (1.0 / cost)
                + self._weights["latency"] * (1000.0 / latency)
                + self._weights["quality"] * quality
            )
            scores.append(score)

        # 加权随机
        total = sum(scores)
        if total <= 0:
            return random.choice(providers)

        r = random.uniform(0, total)
        cumulative = 0.0
        for p, s in zip(providers, scores):
            cumulative += s
            if r <= cumulative:
                return p

        return providers[-1]
