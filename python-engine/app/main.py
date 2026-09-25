# Python AI 引擎入口 — 无状态 FastAPI + 连接池 + 健康检查 + 依赖注入
from __future__ import annotations

import asyncio
import logging
import os
import re
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import TYPE_CHECKING, Any

import redis.asyncio as aioredis
import uvicorn
from fastapi import Depends, FastAPI, Request
from fastapi.responses import JSONResponse, Response, StreamingResponse

from app.config import settings
from app.session_store import SessionStore

if TYPE_CHECKING:
    # 仅第 25 行的模块级注解引用；运行时不需要（from __future__ import annotations
    # 已把注解字符串化），而 app.main 是被大量模块导入的入口，直接导入
    # app.agent.runtime 会形成循环导入。
    from app.agent.runtime import AgentRuntime

# 全局会话消息缓存（lifespan 中接入 Redis 实现多实例共享）
_session_cache = SessionStore(max_sessions=200)
# 活跃 AgentRuntime 注册表（S 安全修复：工具确认端点按 session_id 定位 runtime；
# 值为 (runtime, owner_user_id, run_token)：owner 用于确认时校验来电者身份，
# run_token 用于校验审批属于当前 run（批次 4：归属另镜像到 Redis，见 app/run_registry.py）
_ACTIVE_RUNTIMES: dict[str, tuple[AgentRuntime, str, str]] = {}

logger = logging.getLogger(__name__)

# 全局引用（lifespan 中初始化）
_start_time = time.monotonic()
_redis: aioredis.Redis | None = None
_gateway = None  # GatewayRouter
_queue_worker = None  # asyncio.Task（后台协程句柄）
# QueueWorker 实例：关机时必须先调它的 stop() 优雅排空（停 PEL reclaim、等待进行中任务），
# 再取消上面的 Task。只 cancel Task 会跳过 stop()——start() 内部捕获取消后正常返回。
_queue_worker_instance = None
_mcp_client = None  # MCPClient
# MCPClientPool 实例。必须在这里声明：模块级函数 `touch_user()` 会读它，而赋值发生在
# lifespan 内部（那里用 global）—— 缺这个声明时，任何**早于** lifespan 完成的调用
# （测试、启动早期、初始化失败分支）都会在 touch_user 里 NameError。
_plugin_pool = None


# ── FastAPI 依赖注入 ──


async def get_redis() -> aioredis.Redis:
    """获取 Redis 连接（FastAPI Depends）"""
    if _redis is None:
        raise RuntimeError("Redis not initialized")
    return _redis


async def get_gateway() -> Any:
    """获取 Gateway Router（FastAPI Depends）"""
    if _gateway is None:
        raise RuntimeError("Gateway not initialized")
    return _gateway


def touch_user(user_id: str) -> None:
    """标记用户活跃（有会话/工具/Agent 请求时调用），驱动 MCP 轮询范围。"""
    if _plugin_pool is not None and user_id:
        _plugin_pool._tracker.touch(user_id)  # noqa: SLF001 — 池内专用入口


def verify_sandbox_root() -> None:
    """校验 agent 沙箱根（SANDBOX_ROOT）是否满足部署要求 —— 多副本安全的关键前置。

    背景：所有 agent 文件/命令/git 工具都被强制在
    ``SANDBOX_ROOT/{tenant}/{user}/workspace`` 内执行。该变量的缺省值是
    **进程本地路径**（cwd 上两级），单机开发可用；但多副本部署下，同一用户的任务
    落到不同副本时会各自维护一份 workspace —— 文件/命令工具表现为"间歇性失忆"。

    规则：
    - ``CHIRON_ENV=production|prod`` 时必须显式设置 ``SANDBOX_ROOT``；
      未设置则打印**具体原因与修复指引**后拒绝启动（``CHIRON_ALLOW_LOCAL_SANDBOX=true`` 仅限单机开发放行）。
    - 非生产环境允许缺省，但打印 WARN 说明多副本要求。
    - 已显式配置、但解析结果落在当前工作目录内时给出 WARN（疑似仍是容器本地盘）。
    """
    from app.tools.sandbox import SANDBOX_ROOT_ENV, sandbox_root

    env_name = (os.getenv("CHIRON_ENV") or "").strip().lower()
    is_prod = env_name in ("production", "prod")
    allow_local = (os.getenv("CHIRON_ALLOW_LOCAL_SANDBOX") or "").strip().lower() in (
        "1",
        "true",
        "yes",
        "on",
    )
    raw = (os.getenv(SANDBOX_ROOT_ENV) or "").strip()
    resolved = sandbox_root()

    if raw:
        logger.info("%s=%s (resolved: %s)", SANDBOX_ROOT_ENV, raw, resolved)
        if not allow_local and _is_inside_cwd(resolved):
            logger.warning(
                "%s resolves inside the process working directory (%s). Multi-replica "
                "deployments need a shared volume (NFS/PVC) — otherwise every replica "
                "keeps its own copy of user workspaces.",
                resolved,
                Path.cwd(),
            )
    else:
        logger.warning(
            "%s is not set — agent file/shell/git tools will use the process-local default "
            "(%s). Multi-replica deployments MUST set %s to a shared volume, otherwise "
            "a user's files written on one replica are invisible on another.",
            SANDBOX_ROOT_ENV,
            resolved,
            SANDBOX_ROOT_ENV,
        )

    if is_prod and not raw and not allow_local:
        logger.error(
            "Refusing to start: CHIRON_ENV=%s (production) but %s is not set "
            "(agent sandbox would fall back to the process-local path %s). "
            "Reason: %s/{tenant}/{user}/workspace is where all agent file, shell and git "
            "tools operate; with a per-replica path, a task handled by a different replica "
            "cannot see files written by another (intermittent 'amnesia' in file workflows). "
            "Fix: point %s at a shared volume, e.g. %s=/shared/chiron-sandbox; "
            "for single-node development only, bypass with CHIRON_ALLOW_LOCAL_SANDBOX=true.",
            env_name,
            SANDBOX_ROOT_ENV,
            resolved,
            SANDBOX_ROOT_ENV,
            SANDBOX_ROOT_ENV,
            SANDBOX_ROOT_ENV,
        )
        raise RuntimeError(
            f"{SANDBOX_ROOT_ENV} must point to a shared volume when CHIRON_ENV={env_name} "
            "(set CHIRON_ALLOW_LOCAL_SANDBOX=true for single-node development)"
        )


def verify_media_store() -> None:
    """校验媒体存储后端配置 —— 多副本下媒体文件必须对每个副本可见。

    - ``MEDIA_STORE_BACKEND=s3``：必须提供 ``S3_BUCKET`` 与 ``S3_ENDPOINT_URL``，
      缺失即拒绝启动（缺失会让写入回退到进程本地路径，多副本下生成物只在单副本可见）。
    - ``local``（默认）：打印 WARN 与落盘路径，提示多副本需切 s3。
    """
    backend = (os.getenv("MEDIA_STORE_BACKEND") or "local").strip().lower()

    if backend == "s3":
        missing = [
            key for key in ("S3_BUCKET", "S3_ENDPOINT_URL") if not (os.getenv(key) or "").strip()
        ]
        if missing:
            logger.error(
                "Refusing to start: MEDIA_STORE_BACKEND=s3 but %s missing. "
                "Reason: media writes would fall back to a process-local path, so files "
                "produced on one replica return 404 when served from another. "
                "Fix: set %s (plus S3_ACCESS_KEY/S3_SECRET_KEY when the bucket requires them).",
                ", ".join(missing),
                ", ".join(missing),
            )
            raise RuntimeError(
                "MEDIA_STORE_BACKEND=s3 requires S3_BUCKET and S3_ENDPOINT_URL"
            )
        logger.info(
            "Media store backend: s3 (bucket=%s, prefix=%s)",
            os.getenv("S3_BUCKET"),
            os.getenv("S3_PREFIX", "media/"),
        )
        return

    logger.warning(
        "Media store backend: local (path=%s). Multi-replica deployments should set "
        "MEDIA_STORE_BACKEND=s3 with S3_* so generated media is visible to every replica.",
        os.getenv("MEDIA_STORE_PATH", os.path.join(".", "data", "media")),
    )


def _is_inside_cwd(path: Path) -> bool:
    """判断路径是否位于当前工作目录内（用于识别"仍是容器本地盘"的配置）。"""
    try:
        return path.resolve().is_relative_to(Path.cwd().resolve())
    except Exception:  # noqa: BLE001 - 诊断用途，失败即视为无告警
        return False


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """应用生命周期：启动初始化 + 关闭清理"""
    global _redis, _gateway, _queue_worker

    # ── 0. 全局异常处理 ──
    import sys
    import traceback

    def global_exception_handler(exc_type: Any, exc_value: Any, exc_tb: Any) -> None:
        if issubclass(exc_type, KeyboardInterrupt):
            return
        logger.critical(
            "Unhandled exception",
            exc_info=(exc_type, exc_value, exc_tb),
            extra={
                "traceback": "".join(traceback.format_exception(exc_type, exc_value, exc_tb))
            },
        )

    sys.excepthook = global_exception_handler

    # ── 1. 可观测性 ──
    from app.observability.logging import configure_logging
    from app.observability.metrics import ENGINE_INFO
    from app.observability.tracing import configure_tracing

    configure_logging(settings.log_level)
    configure_tracing(
        service_name="python-engine", otlp_endpoint=settings.otel_endpoint
    )
    ENGINE_INFO.info({"version": "3.0.0", "instance_id": _get_instance_id()})

    logger.info("=" * 60)
    logger.info("Chiron Python AI Engine v3.0 — Enterprise Edition")
    logger.info("=" * 60)

    # ── 1.5 部署前置校验：多副本安全前置（沙箱根 + 媒体后端）──
    # 与下方 Redis 门禁同一策略：多副本下状态必须共享，否则在运行期以
    # "文件工具间歇性失忆"/"媒体下载 404"的形式暴露；这里提前到启动时暴露。
    verify_sandbox_root()
    verify_media_store()

    # ── 2. Redis 连接池 ──
    # 依赖门禁：Redis 是生产必需依赖。未显式开启 DEGRADED_MODE 时，
    # 未配置与连接失败都直接拒绝启动(fail fast)——进程内降级会让多副本看到不同的
    # 会话/限流/队列/事件，问题会在运行期以不一致形式暴露而不是启动时暴露。
    # 仅单机开发允许显式 DEGRADED_MODE=true 走进程内降级。
    if not settings.redis_url:
        if not settings.degraded_mode:
            logger.error(
                "REDIS_URL not configured — refusing to start "
                "(set REDIS_URL, or DEGRADED_MODE=true for single-instance development)"
            )
            raise RuntimeError("REDIS_URL is required unless DEGRADED_MODE=true")
        logger.warning(
            "Redis URL not configured — degraded mode (session cache in-process, distributed features disabled)"
        )
        _redis = None
    else:
        _redis = aioredis.from_url(
            settings.redis_url,
            decode_responses=False,
            max_connections=settings.redis_max_connections,
        )
        try:
            await _redis.ping()
            logger.info(
                "Redis connected: %s (pool=%d)",
                settings.redis_url,
                settings.redis_max_connections,
            )
            # 将 SessionStore 接入 Redis，实现多实例共享
            _session_cache._redis = _redis
            logger.info("SessionStore switched to Redis backend")
        except Exception as e:
            if not settings.degraded_mode:
                logger.error(
                    "Redis unavailable — refusing to start (%s); "
                    "set DEGRADED_MODE=true only for single-instance development",
                    e,
                )
                raise RuntimeError("Redis is required but unavailable") from e
            # 显式降级模式：SessionStore 回退进程内内存模式，依赖 Redis 的功能
            #（分布式限流/会话多实例共享/队列）返回 503（就绪探针见 /readyz）。
            logger.warning(
                "Redis unavailable — degraded mode (session cache in-process, distributed features disabled): %s",
                e,
            )
            _redis = None
    # ── 2.5. PostgreSQL ──
    if settings.postgres_dsn:
        from app.db import ensure_tables, init_pool

        try:
            await init_pool(settings.postgres_dsn)
            await ensure_tables()
            logger.info("PostgreSQL connected and tables ensured")
        except Exception as e:
            logger.warning("PostgreSQL not available: %s", e)

    # ── 3. LLM Gateway ──
    from app.gateway.budget import TokenBudget
    from app.gateway.cache import SemanticCache
    from app.gateway.provider import LLMProvider
    from app.gateway.ratelimit import TenantRateLimiter
    from app.gateway.router import GatewayRouter

    providers: dict[str, LLMProvider] = {}
    # 服务提供商目录（权威源在 Go 网关 internal/api/llm_providers.go，经
    # /v1/internal/engine-config 下发）：provider 注册、端点解析、路由匹配全部目录驱动，
    # 新增提供商无需改引擎代码（见 app/providers/catalog.py 的兜底目录）。
    from app.providers.catalog import (
        provider_api_key,
        provider_base_url,
        provider_catalog,
        provider_kind,
        provider_requires_key,
    )

    _catalog = provider_catalog()
    # KeyRing(DR 集中派):管理端密钥的明文环(Redis keyset 镜像)+ env 种子兜底。
    # provider 在 key_ring 模式下按活跃 key 轮换调用并上报失败。
    from app.gateway.key_ring import KeyRing

    _key_ring = KeyRing(
        redis=_redis,
        env_seeds={
            str(preset["id"]): [seed]
            for preset in _catalog
            if (seed := provider_api_key(preset))
        },
    )
    # DR 集中派(管理端 /v1/admin/api-keys 添加的 key 经网关写入 Redis keyset
    # llm:keys:{provider}):provider 注册条件 = env 种子非空 或 keyset 已存在该 provider 的 key
    # 或 目录标记免 key（Ollama/vLLM 等本地端点）。
    # 仅凭 keyset 时以占位 key 构造(调用时 _resolve_client 会用 keyset 活跃 key 建真实 client)。
    placeholder = "sk-chiron-keyset-managed"

    async def _keyset_has(provider: str) -> bool:
        try:
            return len(await _key_ring.active_keys(provider)) > 0
        except Exception:
            return False

    from app.providers.named import NamedAnthropicProvider, NamedOpenAIProvider

    for _preset in _catalog:
        _pid = str(_preset.get("id") or "")
        if not _pid or _pid in providers:
            continue
        _seed = provider_api_key(_preset)
        if not _seed and provider_requires_key(_preset) and not await _keyset_has(_pid):
            continue
        _base_url = provider_base_url(_preset)
        # 免 key 的**本地自托管** provider（Ollama / vLLM / LM Studio）默认 **不注册**：
        # 它们带的是目录里的本地默认地址（vLLM 是 http://localhost:8000/v1），一旦无条件注册
        # 就会参与路由并在候选里抢走请求，把正常对话打到本机空端口上 ——
        # 表现就是 `provider vllm stream failed: 404 Not Found`（用户明明配的是别的 provider）。
        # 只有用户**显式配置**过（写了 base_url 覆盖，或配了 key/keyset）才注册。
        if not provider_requires_key(_preset):
            _default_base = str(_preset.get("base_url") or "")
            _configured = await _keyset_has(_pid) or (
                _base_url != "" and _base_url != _default_base
            )
            if not _configured:
                logger.info(
                    "skip unconfigured local provider: %s (default base_url=%s)",
                    _pid,
                    _default_base or "<none>",
                )
                continue
        _provider_cls = (
            NamedAnthropicProvider if provider_kind(_preset) == "anthropic" else NamedOpenAIProvider
        )
        providers[_pid] = _provider_cls(
            _pid,
            api_key=_seed or placeholder,
            base_url=_base_url,
            key_ring=_key_ring,
        )
        logger.info(
            "LLM provider registered: %s (kind=%s, base_url=%s)",
            _pid,
            provider_kind(_preset),
            _base_url or "<sdk-default>",
        )

    if not providers:
        logger.warning(
            "No LLM providers configured! Set ANTHROPIC_API_KEY / OPENAI_API_KEY / DEEPSEEK_API_KEY "
            "/ LLM_API_KEY, add a key in admin (服务提供商), or use a no-key provider (Ollama/vLLM)"
        )

    # 目录外的自定义 provider 也要能工作：管理端可为任意 provider 名（如 my-gateway）
    # 添加 key，写入 keyset llm:keys:{provider}。这里扫描 keyset 补齐目录未收录的 provider，
    # 按 OpenAI 兼容协议接入（端点取 DB/env 覆盖）。
    async def _keyset_provider_ids() -> list[str]:
        """扫描 Redis keyset 中已配置 key 的 provider 名。"""
        if _redis is None:
            return []
        from app.redis_keys import rkey

        prefix = rkey("llm:keys:")
        ids: list[str] = []
        try:
            async for key in _redis.scan_iter(match=f"{prefix}*", count=100):
                name = key.decode() if isinstance(key, bytes) else str(key)
                provider = name[len(prefix):] if name.startswith(prefix) else ""
                if provider and provider != "ver" and provider not in ids:
                    ids.append(provider)
        except Exception as exc:  # noqa: BLE001 - 扫描失败不阻断启动
            logger.warning("keyset provider scan failed: %s", exc)
        return ids

    _known_ids = {str(preset.get("id") or "") for preset in _catalog}
    for _pid in await _keyset_provider_ids():
        if not _pid or _pid in providers or _pid in _known_ids:
            continue
        # 合成目录项：模型名带 provider 前缀时（provider/model）可被路由命中
        _custom_preset = {
            "id": _pid,
            "label": _pid,
            "kind": "openai",
            "base_url": "",
            "api_key_env": "",
            "model_prefixes": [f"{_pid}/"],
            "cost": 5.0,
            "quality": 0.80,
            "requires_key": True,
            "model_discovery": True,
        }
        _base_url = provider_base_url(_custom_preset)
        if not _base_url:
            logger.warning(
                "custom provider %s skipped: no base_url (set it in 管理端「服务提供商」或 %s_BASE_URL)",
                _pid,
                _pid.upper().replace("-", "_"),
            )
            continue
        _catalog.append(_custom_preset)
        _known_ids.add(_pid)
        providers[_pid] = NamedOpenAIProvider(
            _pid, api_key=placeholder, base_url=_base_url, key_ring=_key_ring
        )
        logger.info("custom LLM provider registered: %s (base_url=%s)", _pid, _base_url)

    # 创建 embedding 函数（用于语义缓存）
    async def _embed_for_cache(text: str) -> list[float]:
        if "openai" in providers:
            resp = await providers["openai"].embed(text, settings.embedding_model)
            return resp.embedding
        return []

    if _redis is not None:
        cache = SemanticCache(
            redis=_redis,
            embed_fn=_embed_for_cache,
            l1_capacity=settings.cache_l1_capacity,
            l2_ttl=settings.cache_l2_ttl,
            semantic_threshold=settings.semantic_cache_threshold,
            semantic_prefix_dims=settings.semantic_cache_prefix_dims,
        )
        budget = TokenBudget(_redis)
    else:
        cache = None
        budget = None

    _gateway = GatewayRouter(
        providers=providers,
        cache=cache,
        budget=budget,
        gateway_url=settings.gateway_internal_url,
        internal_token=settings.internal_token,
        provider_catalog=_catalog,
    )
    # 同步租户模型路由配置（不阻断启动）
    try:
        routes_count = await _gateway.sync_routes()
        if routes_count > 0:
            logger.info("Tenant model routes synced: %d routes", routes_count)
    except Exception as e:
        logger.warning("Model route sync failed (non-blocking): %s", e)
    logger.info("LLM Gateway: %s providers", ", ".join(providers.keys()) or "none")

    # ── 3.4. 工具/工作流 gateway 注入（六大互通：对话/agent 可调用工作流） ──
    from app.tools.graph import bind_gateway as bind_graph_gateway
    from app.tools.pm import bind_gateway as bind_pm_gateway
    from app.workflow.tools import bind_gateway as bind_workflow_gateway

    bind_graph_gateway(_gateway)
    bind_pm_gateway(_gateway)
    bind_workflow_gateway(_gateway)
    # LLM client（RAG 检索嵌入）同样接入 gateway
    from app.llm.client import llm_client

    llm_client.bind_gateway(_gateway)
    logger.info("Tool/Workflow gateways bound")

    # ── 3.45 记忆服务（L2 档案卡：跨会话长期记忆 + 语义检索）──
    # 依赖 PostgreSQL 连接池与嵌入链路；任一不可用则记忆服务不启用（API 返回 503 fail-loud）
    try:
        from app.agent.prompt_engine import bind_memory_service as bind_prompt_memory
        from app.db import get_pool
        from app.memory.conflict_manager import ConflictManager
        from app.memory.consolidator import Consolidator
        from app.memory.profile import ProfileStore
        from app.memory.profile_card import ProfileCard
        from app.memory.service import MemoryService, set_memory_service
        from app.memory.session_meta import SessionMetaStore
        from app.memory.summaries import SummaryStore as SummaryWriteStore
        from app.memory.summary_store import SummaryStore as SummaryRecallStore

        pool = get_pool()

        # 四层装配各归其位。此前这里把 ProfileStore 装到了 session_meta_store 参数上、
        # 把 Consolidator 装到了 summary_store 参数上 —— 前者导致 L2 条目 API 全走
        # ProfileCard（没有 access_count/status 等列），后者导致 recall/list_summaries
        # 调用的方法在 Consolidator 上根本不存在。
        profile_store = ProfileStore(pool)
        session_meta_store = SessionMetaStore()

        # 两个同名 SummaryStore 职责不同，且操作同一张 memory_summaries 表：
        # - app.memory.summaries.SummaryStore：写入侧，Consolidator 依赖它的
        #   get_by_hash / insert(entry, embedding)
        # - app.memory.summary_store.SummaryStore：读取侧，带查询缓存与向量召回
        # 因此「写入用前者、召回用后者」不会造成数据分裂。
        summary_write = SummaryWriteStore(pool)
        consolidator = Consolidator(store=summary_write, embedder=llm_client.embed)
        # Redis 不可用时降级到写入侧（它也有 list_active，只是没有缓存的 recall）——
        # 让 L3 列表仍能看到数据，而不是整块变成空。
        summary_store = (
            SummaryRecallStore(_redis, embedding_fn=llm_client.embed)
            if _redis is not None
            else summary_write
        )

        # 冲突落 Redis：多副本下任一副本登记的冲突，其余副本都看得到。
        # 进程内那份只是 Redis 不可用时的降级视图。
        conflict_manager = ConflictManager(_redis)
        mem_svc = MemoryService(
            store=profile_store,
            embedder=llm_client.embed,
            summary_store=summary_store,
            consolidator=consolidator,
            session_meta_store=session_meta_store,
            profile_card=ProfileCard(redis=_redis),
            conflict_manager=conflict_manager,
        )
        set_memory_service(mem_svc)
        bind_prompt_memory(mem_svc)
        logger.info(
            "Memory service initialized (L2 entries + profile card + L3 summaries)"
        )
    except Exception as e:
        logger.warning("Memory service not available: %s", e)

    # ── 3.6. 六大工作台能力注册（互通基础：TaskRouter 依赖能力注册中心） ──
    from app.core.capabilities import preload_default_capabilities

    await preload_default_capabilities()

    # ── 4. 限流器（middleware 需要） ──
    # Redis 可用：分布式租户限流；Redis 不可用：本地限流兑底（避免裸奔/None 崩溃）。
    from app.gateway.ratelimit import LocalTenantRateLimiter

    # 两个实现（Redis 版 / 进程内降级版）接口一致但无共同基类，故按 Any 收。
    limiter: Any
    if _redis is not None:
        limiter = TenantRateLimiter(
            redis=_redis,
            requests_per_minute=settings.rate_limit_rpm,
            requests_per_second=settings.rate_limit_rps,
        )
    else:
        # 降级**必须可见**：多副本下进程内限流的额度会放大 N 倍，即全局限流已失效。
        # 日志给人看，指标给告警用 —— 两者都要，否则这次降级只会静静躺在某行日志里。
        from app.observability.metrics import RATE_LIMIT_DEGRADED

        RATE_LIMIT_DEGRADED.inc()
        logger.warning(
            "Redis unavailable: falling back to in-process tenant rate limiter "
            "(single-instance semantics) — global limit is NOT enforced across replicas"
        )
        limiter = LocalTenantRateLimiter(
            requests_per_minute=settings.rate_limit_rpm,
            requests_per_second=settings.rate_limit_rps,
        )
    app.state.limiter = limiter

    # ── 5. MCP Plugin System（用户级连接池：25s 轮询活跃用户配置） ──
    # mcp_pool_enabled=False 时本实例不建立 MCP 连接（多实例部署按节点启用，
    # 避免 N 实例 × 活跃用户 × server 的连接放大；默认开 = 保持单机现状）。
    global _plugin_pool
    if settings.mcp_pool_enabled:
        from app.plugins.pool import MCPClientPool
        from app.plugins.store import ActiveTracker, PluginStore

        # redis 用于 MCP owner 租约（B1a，默认关闭；见 MCP_OWNER_LEASE_ENABLED）
        _plugin_pool = MCPClientPool(
            store=PluginStore(), tracker=ActiveTracker(), redis=_redis
        )
        await _plugin_pool.start()
        logger.info("MCP plugin pool started (poll=%ds)", 25)
    else:
        _plugin_pool = None
        logger.info("MCP plugin pool disabled on this instance (mcp_pool_enabled=false)")

    # ── 6. 子 Agent 运行期治理：必须独立于 Redis 可用性启动 ──
    #
    # 看门狗（空闲/超时自动中止）治理的是**进程内注册表**，不需要 Redis；
    # 此前它挂在 _run_queue_worker 里，于是 Redis 不可用时连"本进程的子 Agent 卡住了"
    # 都没人管 —— 而卡住恰恰是 Redis 抖动时最容易发生的事。
    # 取消订阅需要 Redis（跨实例广播），单独在可用时启动。
    from app.subagent import registry as _subagent_registry

    _subagent_registry.start_watchdog()
    if _redis is not None:
        _subagent_registry.start_cancel_subscriber()
    else:
        logger.warning(
            "subagent cancel subscriber NOT started (no Redis): 跨实例取消不可用，"
            "本实例的子 Agent 只能靠看门狗按空闲/超时收口"
        )

    # ── 7. 启动 Queue Worker ──
    if _redis is not None:
        _queue_worker = asyncio.create_task(_run_queue_worker(_redis, _gateway))

    # 拉起上次入队失败的工作流（X3）：它们在 DB 里是 queued_pending，不拉起就永远不会跑。
    # 放在队列 worker **之后** —— 先有消费者，再补投递。
    try:
        from app.api.workflows import requeue_pending_workflows

        if await requeue_pending_workflows():
            logger.info("workflow requeue at startup completed")
    except Exception as exc:  # noqa: BLE001 - 拉起失败不该阻断启动
        logger.warning("workflow requeue at startup failed: %s", exc)
        logger.info(
            "Queue worker started (concurrency=%d)", settings.queue_worker_concurrency
        )
    else:
        logger.info("Queue worker skipped (Redis not available)")

    # ── 6.2 幂等表保留策略（A7/C1）──
    # task_idempotency 每任务一行，长期运行必须清理；turns 由网关侧
    # src: internal/api/retention.go 的 StartRetentionCleaner 负责。
    _retention_task = asyncio.create_task(_run_retention_cleaner())

    # ── 6.5. 启动进程指标收集器 ──
    from app.observability.metrics import record_process_metrics

    async def metrics_collector() -> None:
        """定期收集进程资源指标"""
        while True:
            try:
                record_process_metrics()
            except Exception as e:
                logger.warning(f"Failed to record process metrics: {e}")
            await asyncio.sleep(10)

    _metrics_task = asyncio.create_task(metrics_collector())
    logger.info("Process metrics collector started (interval=10s)")

    # ── 8. 实例注册（批 E1：引擎动态发现）──
    # 网关 StartEngineDiscovery 每 15s 消费本注册表并动态更新引擎地址（替代静态清单）。
    # 需 ENGINE_ADVERTISE_URL（引擎网络可达地址）;Redis 不可用/未配置时跳过,网关回退静态地址。
    # A4：run 归属路由（engine:run:*，批 4）依赖本注册表才能把 instance_id 解析成地址——
    # 未配置时归属映射会被写入但网关查不到地址，审批/取消仍按一致性哈希漂移。
    # 这不是故障，但必须让部署者知道"亲和是尽力而为"，而不是以为已按归属路由。
    if _redis is not None and not settings.engine_advertise_url:
        logger.warning(
            "ENGINE_ADVERTISE_URL not set: engine will not register, so run-owner "
            "affinity falls back to session hash (approvals/cancel may drift after "
            "scale-out or instance restart)"
        )
    from app.engine_registry import EngineRegistry

    _engine_registry = EngineRegistry(
        redis=_redis,
        instance_id=_get_instance_id(),
        advertise_url=settings.engine_advertise_url,
        version="3.0.0",
    )
    await _engine_registry.start()

    logger.info("=" * 60)
    logger.info("Ready. HTTP port: %d", settings.http_port)
    logger.info("=" * 60)

    yield  # ── 应用运行中 ──

    # ── 关闭 ──
    logger.info("Shutting down...")

    # 停止队列 worker：先优雅排空，再取消后台协程。
    # 不能只 cancel：QueueWorker.start() 内部捕获 CancelledError 后正常返回，
    # 因此 stop()（停止领取新任务、停止 PEL reclaim loop、最多等待 30s 进行中任务）
    # 会被整体跳过，进行中的任务随事件循环被直接取消、消息滞留在 PEL 等待 reclaim。
    if _queue_worker:
        if _queue_worker_instance is not None:
            try:
                await _queue_worker_instance.stop()
            except Exception as e:  # noqa: BLE001 - 排空失败不应阻断后续关闭步骤
                logger.warning("Queue worker drain failed: %s", e)
        _queue_worker.cancel()
        try:
            await _queue_worker
        except asyncio.CancelledError:
            pass

    # 停止进程指标收集器
    if '_metrics_task' in locals():
        _metrics_task.cancel()
        try:
            await _metrics_task
        except asyncio.CancelledError:
            pass

    # 停止保留策略清理（A7/C1）
    if '_retention_task' in locals():
        _retention_task.cancel()
        try:
            await _retention_task
        except asyncio.CancelledError:
            pass

    # 等待后台任务完成（上下文巩固等）
    from app.context.manager import wait_background_tasks

    await wait_background_tasks(timeout=10.0)

    # 关闭 PostgreSQL
    from app.db import close_pool

    await close_pool()

    # 停止实例注册心跳（批 E1；优雅退出时主动注销）
    if "_engine_registry" in locals() and _engine_registry:
        await _engine_registry.stop()

    # 关闭 MCP 插件池
    if _plugin_pool:
        await _plugin_pool.stop()
        _plugin_pool = None

    # 关闭 Gateway
    if _gateway:
        try:
            await _gateway.close()
        except Exception as e:
            logger.warning("Gateway close error: %s", e)

    # 关闭 Redis
    if _redis:
        await _redis.close()

    logger.info("Shutdown complete")


def _get_instance_id() -> str:
    """本实例标识。

    唯一实现是 ``app/subagent/affinity.py``（P4）：它必须**进程内稳定** —— 归属映射按它
    区分实例，若每次调用都换名字（兜底分支带随机后缀），网关看到的映射会自相矛盾。
    这里只做转发，避免"两处各算一次"漂移。
    """
    from app.subagent.affinity import cached_instance_id

    return cached_instance_id()


# ── 附件内容注入：自动下载文件并注入到 LLM 上下文中 ──

_MEDIA_URL_RE = re.compile(r"(!?)\[([^\]]+)\]\(([^)]+)\)")


async def _resolve_attachments(content: str) -> str:
    """解析用户消息中的附件 Markdown 链接，下载文件内容并注入到消息文本中。

    支持：
    - Markdown 图片 ![](url) 和普通链接 [name](url)
    - 文本类文件（.txt, .md, .csv, .json, .py 等）：自动下载并注入内容
    - PDF 文件：提取文本内容
    - 图片文件：保留原链接并添加说明

    失败时优雅退化——保留原始链接，LLM 仍可通过 web_fetch 工具访问。
    """
    if not content:
        return content

    matches = _MEDIA_URL_RE.findall(content)
    if not matches:
        return content

    import httpx

    from app.config import settings
    from app.tools.ssrf import fetch_url_safe

    # S 安全修复：禁用自动重定向，改用 ssrf.fetch_url_safe 逐跳校验
    # scheme/端口/DNS/IP（含重定向绕过）后再获取，防止附件 URL 打内网/云元数据。
    async with httpx.AsyncClient(timeout=settings.http_timeout_web, follow_redirects=False) as client:
        for is_image, name, url in matches:
            try:
                resp = await fetch_url_safe(client, url)
                if resp.status_code != 200:
                    continue

                content_type = resp.headers.get("content-type", "") or ""
                file_ext = name.rsplit(".", 1)[-1].lower() if "." in name else ""

                # ── 文本类文件：直接注入内容 ──
                if content_type.startswith("text/") or file_ext in (
                    "txt",
                    "md",
                    "csv",
                    "json",
                    "xml",
                    "yaml",
                    "yml",
                    "py",
                    "js",
                    "ts",
                    "go",
                    "java",
                    "c",
                    "cpp",
                    "h",
                    "rs",
                    "sh",
                    "bat",
                    "ps1",
                    "sql",
                    "html",
                    "css",
                    "toml",
                    "ini",
                    "cfg",
                    "conf",
                    "log",
                ):
                    text = resp.text
                    MAX_CHARS = 8000  # noqa: N806 — 局部阈值常量，沿用大写惯例
                    snippet = text[:MAX_CHARS]
                    file_block = (
                        f"\n\n===== 附件「{name}」内容 ({(len(text))} 字符) ====\n"
                        f"{snippet}"
                    )
                    if len(text) > MAX_CHARS:
                        file_block += f"\n... (已截断，仅显示前 {MAX_CHARS} 字符)"
                    file_block += "\n===== 附件结束 ====="
                    content = content.replace(
                        f"{'!' if is_image else ''}[{name}]({url})", file_block
                    )

                # ── PDF：尝试提取文本 ──
                elif content_type == "application/pdf" or file_ext == "pdf":
                    try:
                        import pymupdf

                        doc: Any = pymupdf.open(stream=resp.content, filetype="pdf")
                        pdf_text = "\n".join(page.get_text() for page in doc)
                        doc.close()
                        MAX_PDF_CHARS = 8000  # noqa: N806 — 同上
                        snippet = pdf_text[:MAX_PDF_CHARS]
                        file_block = (
                            f"\n\n===== 附件「{name}」内容 (PDF, {len(pdf_text)} 字符) ====\n"
                            f"{snippet}"
                        )
                        if len(pdf_text) > MAX_PDF_CHARS:
                            file_block += (
                                f"\n... (PDF 较长，已截断前 {MAX_PDF_CHARS} 字符)"
                            )
                        file_block += "\n===== 附件结束 ====="
                        content = content.replace(
                            f"{'!' if is_image else ''}[{name}]({url})", file_block
                        )
                    except Exception:
                        # PDF 解析失败，保留原始链接
                        pass

                # ── 图片：保留 Markdown 格式，添加说明 ──
                elif content_type.startswith("image/"):
                    content = content.replace(
                        f"![{name}]({url})",
                        f"![{name}]({url})\n[图片附件：{name}]",
                    )

                # ── 其他二进制文件：尝试作为文本读取 ──
                else:
                    try:
                        text = resp.text
                        if text and len(text) > 20:
                            MAX_CHARS = 4000  # noqa: N806 — 同上
                            snippet = text[:MAX_CHARS]
                            file_block = (
                                f"\n\n===== 附件「{name}」内容 ====\n" f"{snippet}"
                            )
                            if len(text) > MAX_CHARS:
                                file_block += "\n... (已截断)"
                            file_block += "\n===== 附件结束 ====="
                            content = content.replace(f"[{name}]({url})", file_block)
                    except Exception:
                        pass

            except Exception as e:
                logger.warning("解析附件失败: %s — %s", url, e)
                continue

    return content


def _setup_middleware(app: FastAPI, redis: aioredis.Redis, limiter: Any) -> None:
    """注册中间件链（注意：FastAPI 后注册的先执行）"""
    from app.middleware.auth import AuthMiddleware
    from app.middleware.error_handler import ErrorHandlerMiddleware
    from app.middleware.metrics import MetricsMiddleware
    from app.middleware.rate_limit import RateLimitMiddleware
    from app.middleware.request_context import RequestContextMiddleware

    # 执行顺序: RequestContext → Auth → RateLimit → Metrics → ErrorHandler → handler
    app.add_middleware(ErrorHandlerMiddleware)
    app.add_middleware(MetricsMiddleware)
    app.add_middleware(RateLimitMiddleware, limiter=limiter)
    app.add_middleware(
        AuthMiddleware,
        redis_client=redis,
        jwt_secret=settings.jwt_secret,
        internal_token=settings.internal_token,
    )
    app.add_middleware(RequestContextMiddleware)


def _setup_routes(app: FastAPI) -> None:
    """注册所有 HTTP 路由"""
    import time as _time

    # ── 健康检查 ──

    @app.get("/healthz")
    async def healthz() -> dict[str, Any]:
        return {"status": "ok"}

    @app.get("/metrics")
    async def metrics() -> Response:
        """Prometheus 抓取端点。

        此前指标只在 `app/observability/metrics.py` 里**定义**，没有任何导出路由 ——
        于是 `queue_depth` / `queue_dlq_total` / `token_budget_*` 等全部没有采集入口，
        多实例压测时看不到队列积压与吞吐，等于盲测；而 `prometheus_alerts.yml` 里
        已经引用了 `up{job="python-engine"}`，那条规则一直不可能成立。

        鉴权：`/metrics` 已在 middleware 白名单里（`app/middleware/auth.py`），
        因此这里不需要额外令牌 —— 生产环境请用网络策略/仅内网暴露来隔离。
        """
        from fastapi import Response
        from prometheus_client import CONTENT_TYPE_LATEST, generate_latest

        return Response(content=generate_latest(), media_type=CONTENT_TYPE_LATEST)

    @app.get("/readyz")
    async def readyz() -> Any:
        """K8s readiness: Redis + 至少一个 Provider 可用"""
        if _redis is None:
            return JSONResponse(
                {"status": "not_ready", "reason": "redis not available"},
                status_code=503,
            )
        try:
            await _redis.ping()
            return {"status": "ready"}
        except Exception:
            return JSONResponse(
                {"status": "not_ready", "reason": "redis ping failed"}, status_code=503
            )

    @app.get("/info")
    async def info() -> dict[str, Any]:
        from app.observability.metrics import get_active_requests, get_process_resources

        cpu_percent, memory_mb = get_process_resources()
        return {
            "version": "3.0.0",
            "instance_id": _get_instance_id(),
            "uptime_seconds": int(_time.monotonic() - _start_time),
            "active_tasks": get_active_requests(),
            "cpu_percent": round(cpu_percent, 2),
            "memory_mb": round(memory_mb, 2),
            "gateway": _gateway.stats() if _gateway else None,
        }

    @app.get("/healthz/providers")
    async def healthz_providers() -> Any:
        """Provider 健康检查：逐个调用 probe 并返回状态"""
        if _gateway is None:
            return JSONResponse(
                {"status": "not_ready", "reason": "gateway not initialized"},
                status_code=503,
            )
        try:
            provider_health = await _gateway.health_check()
            all_ok = all(v["status"] == "ok" for v in provider_health.values())
            status = "ok" if all_ok else "degraded"
            return {"status": status, "providers": provider_health}
        except Exception as e:
            return JSONResponse(
                {"status": "error", "message": str(e)}, status_code=500
            )

    # ── Agent 推理（模块级路由函数） ──
    app.post("/v1/agent/run")(agent_run)
    app.post("/v1/agent/submit")(agent_submit)
    app.post("/v1/agent/approval")(agent_approval)
    app.post("/v1/agent/answer")(agent_answer)

    # ── 知识库（模块级路由函数） ──
    app.post("/v1/kb/build")(kb_build)
    app.post("/v1/kb/query")(kb_query)

    # ── 网关管理（模型热切换、熔断器重置） ──
    @app.post("/v1/gateway/sync-routes")
    async def gateway_sync_routes() -> Any:
        """运行时热同步租户模型路由配置（从 Go 网关拉取）。

        不阻断调用，失败时保留上次同步的配置。
        """
        if _gateway is None:
            return JSONResponse(
                {"status": "error", "message": "gateway not initialized"},
                status_code=503,
            )
        try:
            count = await _gateway.sync_routes()
            return {"status": "ok", "routes_synced": count}
        except Exception as e:
            return JSONResponse(
                {"status": "error", "message": str(e)}, status_code=500
            )

    @app.post("/v1/gateway/reset-breaker")
    async def gateway_reset_breaker(request: Request) -> Any:
        """重置指定 Provider 的熔断器。

        Request body: {"provider": "openai"}
        常用于管理端手动恢复因网络抖动而熔断的 Provider。
        """
        if _gateway is None:
            return JSONResponse(
                {"status": "error", "message": "gateway not initialized"},
                status_code=503,
            )
        try:
            body = await request.json()
            provider = body.get("provider", "")
            if not provider:
                return JSONResponse(
                    {"status": "error", "message": "provider is required"},
                    status_code=400,
                )
            ok = await _gateway.reset_breaker(provider)
            if not ok:
                return JSONResponse(
                    {"status": "error", "message": f"provider '{provider}' not found"},
                    status_code=404,
                )
            return {"status": "ok", "provider": provider}
        except Exception as e:
            return JSONResponse(
                {"status": "error", "message": str(e)}, status_code=500
            )

    # ── Tools API（Phase 1） ──
    from app.api import api_router

    app.include_router(api_router)

    # ── Admin API Keys（模块级路由函数） ──
    # DR 集中派：管理端密钥管理已迁移至 Go 网关本地实现(/v1/admin/api-keys)。
    # 引擎侧 admin_* 函数保留(兼容旧测试)但不再注册路由，避免与网关 keyset 双写分裂。


# ── 模块级路由处理函数（FastAPI 需在模块作用域才能正确推断 body 类型） ──


async def agent_run(
    request: Request,
    gateway: Any = Depends(get_gateway),
) -> Any:
    """流式 Agent 推理 — SSE 输出"""
    import json

    from app.agent.loop import run_agent

    body = await request.json()
    llm_config = body.get("llm_config") or {}
    provider_hint = llm_config.get("provider", "")

    async def event_generator() -> AsyncIterator[Any]:
        try:
            async for event in run_agent(
                gateway=gateway,
                system_prompt=body.get("system_prompt", ""),
                history=body.get("history", []),
                content=body.get("content", ""),
                tools=body.get("tools") or None,
                llm_config=llm_config,
                max_turns=(
                    (body.get("max_turns") or 0)
                    if body.get("max_turns", 0) > 0
                    else None
                ),
                tenant_id=body.get("tenant_id", ""),
                provider_hint=provider_hint,
            ):
                yield f"data: {json.dumps(event, ensure_ascii=False)}\n\n"
        except asyncio.CancelledError:
            # 客户端断开/网关取消：流式生成器被 ASGI 取消 → 中止 agent 循环，不吞取消
            logger.info(
                "Agent run stream cancelled (client disconnected)",
                extra={"session_id": body.get("session_id", "")},
            )
            raise
        except Exception as e:
            logger.error("Agent run error: %s", e)
            yield f"data: {json.dumps({'type': 'error', 'message': str(e)})}\n\n"

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


async def _workflow_context_stream(
    graphs: list[tuple[str, dict[str, Any]]],
    body: dict[str, Any],
    gateway: Any,
) -> AsyncIterator[str]:
    """执行选中的工作流链，并以 SSE 事件下发结果。

    事件形态与 AgentRuntime 保持一致：结果走 ``text``（Go 网关只对 ``text`` 事件
    累加最终内容并落库，换成别的类型会表现为"对话里看得见、刷新就没了"），
    末尾补 ``done`` 收尾。
    """
    import json
    import uuid

    from app.api.unified_executor import run_workflow_graphs

    trace_id = uuid.uuid4().hex[:12]
    try:
        result = await run_workflow_graphs(
            graphs, str(body.get("content", "")), trace_id, gateway
        )
        if result.get("status") == "error":
            output = result.get("output")
            message = (
                output.get("error", "workflow execution failed")
                if isinstance(output, dict)
                else "workflow execution failed"
            )
            yield f"data: {json.dumps({'type': 'error', 'content': message}, ensure_ascii=False)}\n\n"
            return

        output = result.get("output")
        text = output.get("result", "") if isinstance(output, dict) else ""
        if text:
            yield f"data: {json.dumps({'type': 'text', 'content': text}, ensure_ascii=False)}\n\n"
        yield f"data: {json.dumps({'type': 'done'}, ensure_ascii=False)}\n\n"
    except asyncio.CancelledError:
        # 客户端断开/网关取消：与 AgentRuntime 同口径，不吞取消
        logger.info("Workflow context stream cancelled (client disconnected)")
        raise
    except Exception as e:  # noqa: BLE001
        logger.error("Workflow context stream error: %s", e)
        yield f"data: {json.dumps({'type': 'error', 'content': str(e)}, ensure_ascii=False)}\n\n"


async def agent_submit(
    request: Request,
    gateway: Any = Depends(get_gateway),
) -> Any:
    """Go 网关代理端点 — 完整 ReAct 循环，SSE 输出"""
    import json

    body = await request.json()

    # ── 工作流关联目标（SSE 链路）──
    # "在工作流页点『在对话中使用』"走的是这条链路（ChatView 的 query 无 task 时
    # 即 unifiedMode=false）。此前这里没有任何 workflow 消费点，工作流被静默丢弃 ——
    # 无报错、无日志，用户只看到"点了没反应"。目标解析与统一链路共用同一实现
    # （app/agent/workbench_context.py 的 selected_workflow_ids）。
    from app.agent.workbench_context import selected_workflow_ids

    workflow_ids = selected_workflow_ids(body.get("context") or {})
    if workflow_ids:
        from app.api.unified_executor import load_selected_workflows

        graphs = await load_selected_workflows(workflow_ids)
        if graphs:
            return StreamingResponse(
                _workflow_context_stream(graphs, body, gateway),
                media_type="text/event-stream",
                headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
            )
        # 全部载不到（已删 / 越权 / DB 不可用）：落回 AgentRuntime，不阻断对话
        logger.warning(
            "workflow context: none of %s loaded; falling back to agent runtime",
            workflow_ids,
        )

    import app.tools.agent  # noqa: F401 — AGENTS 工作台 (agent_dispatch 等)
    import app.tools.browser  # noqa: F401 — 浏览器自动化 (PLUGINS/MCP 扩展)
    import app.tools.core  # noqa: F401 — 确保核心工具已注册
    import app.tools.edit_file  # noqa: F401 — 文件编辑 (创造模式常用)
    import app.tools.jobs  # noqa: F401 — 后台任务
    import app.tools.kb  # noqa: F401 — KNOWLEDGE 工作台 (kb_list/kb_search)
    import app.tools.memory  # noqa: F401 — 长期记忆 (跨工作台共享上下文)
    import app.tools.mode_admin  # noqa: F401 — 创造模式
    import app.tools.run_code  # noqa: F401 — PTC 模式

    # ── 六大工作台互联互通：注册各工作台工具,使 CHAT 的 LLM 可通过 function-calling 调用 ──
    import app.tools.skill  # noqa: F401 — SKILLS 工作台 (skill_list/skill_run/skill_install)
    import app.tools.subagent  # noqa: F401 — 多 agent 委派工具
    import app.tools.subagent_list  # noqa: F401 — 主 Agent 主动感知 (list_subagent_runs)
    import app.tools.subagent_rerun  # noqa: F401 — 重跑已结束的子 Agent (rerun_subagent)
    import app.tools.subagent_result  # noqa: F401 — 子 Agent 结果按需读取 (read_subagent_result)
    import app.tools.terminal  # noqa: F401 — 持久终端
    import app.tools.web  # noqa: F401 — 网页搜索/抓取
    import app.workflow.tools  # noqa: F401 — WORKFLOW 工作台 (workflow_run/workflow_list)
    from app.agent.runtime import AgentRuntime, AgentTask

    # PLUGINS 工作台: MCP 工具由 app.plugins.pool / app.mcp.registry 动态注册,启动时已加载

    # ── 解析附件文件内容并注入到用户消息中 ──
    raw_content = body.get("content", "")
    resolved_content = await _resolve_attachments(raw_content)
    body["content"] = resolved_content

    # ── 身份：优先信任网关注入的 X-User-ID（S2 安全修复） ──
    # Python 端口仅应经 Go 网关可达；直连时若缺失 header 才回退 body（不信任 body 伪造）
    gw_user = request.headers.get("x-user-id", "")

    task = AgentTask(
        id=f"submit_{int(time.time())}",
        tenant_id=body.get("tenant_id", ""),
        user_id=gw_user or body.get("user_id", ""),
        session_id=body.get("session_id", ""),
        content=body.get("content", ""),
        history=body.get("history", []),
        max_turns=max(
            1, min(body.get("max_turns") or settings.max_turns, settings.max_turns)
        ),
        # 工作台上下文（网关透传）：知识库 / Agent / 技能 / 工作流
        workbench_context=body.get("context") or {},
    )

    # ── 工作台上下文：Agent 覆盖 system_prompt / max_turns / model ──
    # 用户在对话里显式选了 Agent，就以它的设定为准（prompt_engine 会把
    # task.system_prompt 当 base，再把工具/技能/RAG 段落追加在后面）。
    workbench_context = body.get("context") or {}
    agent_conf = workbench_context.get("agent") if isinstance(workbench_context.get("agent"), dict) else None
    if agent_conf:
        if agent_conf.get("system_prompt"):
            task.system_prompt = str(agent_conf["system_prompt"])
        max_turns_override = agent_conf.get("max_turns")
        if isinstance(max_turns_override, (int, float)) and max_turns_override > 0:
            task.max_turns = max(1, min(int(max_turns_override), settings.max_turns))
        # Agent 自带的工具集。runtime 见 task.tools 非空就只放这些工具
        # （runtime.py 的 _convert_tools 替换核心工具集），所以这一步让
        # "带 Agent 进对话"不再只带人格、不带能力。
        agent_tools = agent_conf.get("tools")
        if isinstance(agent_tools, list):
            task.tools = [t for t in agent_tools if isinstance(t, dict)]
        # 多选的其余 Agent → 可委派专家：写进 system prompt，让模型知道能请教谁
        # （具体怎么用见 app/tools/subagent.py 的 expert 参数）。这里只做描述，
        # 真正的授权在 subagent 工具里按清单名字校验。
        experts = agent_conf.get("experts")
        if isinstance(experts, list):
            roster = [
                f"- {item.get('name')}：{item.get('description') or '（无描述）'}"
                for item in experts
                if isinstance(item, dict) and item.get("name")
            ]
            if roster:
                task.system_prompt = (
                    f"{task.system_prompt}\n\n## 可委派的专家\n"
                    "需要时用 subagent 工具并指定 expert 参数向下列专家请教：\n"
                    + "\n".join(roster)
                )

    # ── 深度推理模式：设置 system_prompt 要求输出思考过程 ──
    llm_config = body.get("llm_config", {}) or {}
    if agent_conf and agent_conf.get("model"):
        llm_config["model"] = str(agent_conf["model"])
    if llm_config.get("deep_reasoning"):
        reasoning_note = (
            "First output your reasoning process inside "
            "[thinking]...[/thinking] tags, then output your final concise answer.\n"
            "Example: [thinking]I need to analyze...[/thinking]The answer is..."
        )
        # Agent 已定义角色时追加而非覆盖，避免把用户 Agent 的提示词冲掉
        task.system_prompt = (
            f"{task.system_prompt}\n\n{reasoning_note}"
            if task.system_prompt
            else f"You are Chiron. {reasoning_note}"
        )
        # 深度模式需要更大的输出 token 预算以容纳思考过程
        if "max_tokens" not in llm_config:
            llm_config["max_tokens"] = 8192
        task.llm_config = llm_config
    else:
        if not task.system_prompt:
            task.system_prompt = (
                "You are Chiron. Reply briefly in Chinese. "
                "When the user says 'this code' / '这段代码' / '上面的代码', they mean "
                "the code you generated in previous turns of this conversation — use it "
                "directly, don't ask them to re-paste it. "
                "You can save files with the write_file tool. "
                "When the user says '媒体库' / 'media library', they mean the "
                "media directory inside your sandbox workspace (create it with "
                "mkdir if needed); you only have access to your own sandbox "
                "workspace — never use absolute paths or try to access "
                "directories outside it (they are blocked). "
                "Code or text files can be saved there too — just save the file, "
                "don't refuse because it isn't an image/video/audio."
            )
        task.llm_config = llm_config

    # ── 子 Agent 主动汇报：把"自上次交互以来结束的后台子任务"注入本轮上下文 ──
    #
    # 为什么不能只靠 followup：那条链是"队列 → 网关 → 新一轮"，每一跳都能静默丢
    # （速率上限 / 级联保护 / Redis 不可用 / deadline 进 DLQ）。这里改走**查询**：
    # 终态在 DB（唯一权威），父会话下一轮开始时按游标增量取回 —— 即使 followup 全丢，
    # 结论也不会消失。详见 app/subagent/reporting.py。
    #
    # 注入失败绝不影响本轮（报告是增强，不是前提）；但必须可见（记 warning）。
    try:
        from app.subagent.reporting import consume_pending_reports, format_reports

        reports = await consume_pending_reports(
            session_id=getattr(task, "session_id", "") or "",
            tenant_id=getattr(task, "tenant_id", "") or "",
            user_id=getattr(task, "user_id", "") or "",
        )
        if reports:
            block = format_reports(reports)
            task.system_prompt = (
                f"{task.system_prompt}\n\n{block}" if task.system_prompt else block
            )
            logger.info(
                "subagent reports injected: session=%s count=%d",
                getattr(task, "session_id", ""), len(reports),
            )
    except Exception as exc:  # noqa: BLE001 - 注入失败不阻断对话
        logger.warning("subagent reports injection failed: %s", str(exc)[:160])

    # ── 运行模式（常规/极简/PTC/创造）：前端下拉 → body.mode 或 llm_config.mode ──
    # runtime 内 get_mode_config 兜底未知值回退 NORMAL
    mode = body.get("mode") or llm_config.get("mode")
    if mode:
        task.llm_config = {**task.llm_config, "mode": mode}

    # 注入记忆服务（L2 档案卡 + L3 摘要），不可用时 None（行为不变）
    from app.memory.service import get_service as get_memory_service

    runtime = AgentRuntime(
        gateway=gateway,
        session_store=_session_cache,
        memory=get_memory_service(),
    )
    session_id = task.session_id
    run_lease = None
    if session_id:
        import uuid

        from app.run_registry import RunLease

        run_token = uuid.uuid4().hex
        _ACTIVE_RUNTIMES[session_id] = (runtime, task.user_id, run_token)
        # run 归属映射（Redis）：网关据此把该 session 的请求路由到本实例，
        # 审批端点据此校验归属与 run_token。实例故障后映射随 TTL(300s) 过期，
        # 用户重试即在新实例重建 run（现场状态不迁移，明确中断而非静默错路由）。
        run_lease = RunLease(
            _redis,
            session_id,
            _get_instance_id(),
            run_token,
            owner_uid=task.user_id or "",
            url=settings.engine_advertise_url,
        )

    async def event_generator() -> AsyncIterator[Any]:
        total_in = 0
        total_out = 0
        started = time.monotonic()

        # ── 子 Agent 事件旁路（docs/subagent-design.md §4.3）──
        # 子 Agent 在工具调用内部运行，其进度需"穿透"到这条父 SSE 流：经 contextvar
        # 暴露 EventSink，再把 runtime 事件与旁路事件合并输出。旁路有界+限流，绝不阻塞子 Agent。
        from app.agent.event_sink import EventSink
        from app.tools.context import set_tool_context

        # session_id 注入到每条事件里：前端 SSE 按会话过滤，缺了会被投给所有订阅者（串扰）
        sink = EventSink(session_id=getattr(task, "session_id", "") or "")
        set_tool_context(event_sink=sink)
        merged: asyncio.Queue[Any] = asyncio.Queue(maxsize=1024)

        async def _pump_runtime() -> None:
            try:
                async for ev in runtime.run(task):
                    await merged.put(("runtime", ev))
            finally:
                await merged.put(("eof", None))

        def _frame(payload: dict[str, Any]) -> str:
            return f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"

        # 运行期缓存（Redis，TTL 1h）：写入的事件与发给前端的完全一致 —— 既保证
        # "侧边栏回放 == 实时流"，也天然复用了旁路的限流（不产生写放大）。
        runtime_cache = None
        try:
            from app.subagent.runtime_cache import get_runtime_cache

            runtime_cache = await get_runtime_cache()
        except Exception as cache_err:  # noqa: BLE001 - 缓存不可用不影响主流程
            logger.debug("subagent runtime cache unavailable: %s", cache_err)
        cache_tenant = getattr(task, "tenant_id", "") or "default"

        # 运行期缓存（Redis）的写入改由**常驻投递器**负责：它挂在 sink 身上，父 turn 结束后
        # （后台委派的典型场景）仍在投递 —— 否则 `run_in_background=True` 的子 Agent 在父
        # 生成器退出后的所有事件都无处可去，前端面板与 /events 端点永远空白。
        from app.subagent import registry as subagent_registry

        async def _persist_subagent_event(payload: Any) -> None:
            run_id = payload.get("run_id") or ""
            if run_id:
                # 事件即心跳：看门狗据此认定"仍在产出内容的 run"没有卡死
                subagent_registry.touch(run_id)
            if runtime_cache is not None:
                if run_id:
                    # ① Stream：供**历史回放**（Redis 过期后由 DB steps 兜底）
                    await runtime_cache.push_event(run_id=run_id,
                                                   tenant=cache_tenant, payload=payload)
                # ② pub/sub：供**实时**推送 —— 网关订阅后转投 SSE hub。
                #    缺了它，前端只剩"选中某个 run 时 3s 轮询"这一条路。
                await runtime_cache.publish_live_event(payload=payload)

        # 无论 Redis 是否可用都注册投递器：心跳不能因为缓存不可用就停
        sink.attach_persistent(_persist_subagent_event)

        async def _sink_frames() -> AsyncIterator[str]:
            """把旁路事件转成 SSE 帧（落缓存已交给上面的常驻投递器，避免双写）。"""
            for sub_event in sink.drain():
                yield _frame(sub_event.to_payload())

        pump = asyncio.create_task(_pump_runtime())
        try:
            if run_lease is not None:
                await run_lease.start()
            while True:
                # 1) 先冲刷子 Agent 旁路（保证进度实时性）
                async for frame in _sink_frames():
                    yield frame
                # 2) 取下一个来源事件；两处都空则短等，避免忙等
                try:
                    kind, event = merged.get_nowait()
                except asyncio.QueueEmpty:
                    await asyncio.wait({pump}, timeout=0.1)
                    async for frame in _sink_frames():
                        yield frame
                    try:
                        kind, event = merged.get_nowait()
                    except asyncio.QueueEmpty:
                        continue
                if kind == "eof":
                    break
                if event.input_tokens:
                    total_in += event.input_tokens
                if event.output_tokens:
                    total_out += event.output_tokens
                yield _frame({'type': event.type, 'content': event.content or event.error, 'id': event.tool_call_id, 'name': event.tool_name, 'arguments': event.tool_arguments, 'options': event.options, 'input_tokens': event.input_tokens, 'output_tokens': event.output_tokens, 'cached_tokens': event.cached_tokens, 'model': event.model})
            # 收尾：把旁路中剩余的预览与唯一终态送出
            async for frame in _sink_frames():
                yield frame
            # 正常收尾 → Webhook agent.complete（主对话收尾统一出口；失败不影响主流程）
            if session_id:
                try:
                    from app.event_bus import emit_agent_complete

                    await emit_agent_complete(
                        session_id, task.user_id or "", task.tenant_id or "default",
                        tokens_used=total_in + total_out,
                        duration_ms=int((time.monotonic() - started) * 1000),
                    )
                except Exception as wh_err:  # noqa: BLE001
                    logger.debug("agent webhook emit failed: %s", wh_err)
        except asyncio.CancelledError:
            # 客户端断开/网关取消：ASGI 取消流式生成器 → 取消传播中止 agent 循环（无 shield）。
            # 不吞取消：重新抛出，保持 finally（_ACTIVE_RUNTIMES 清理等）正常执行。
            logger.info(
                "Agent submit stream cancelled (client disconnected)",
                extra={"session_id": session_id},
            )
            # ⚠️ 这里**不再**连带停掉后台子 Agent。
            #
            # 这条分支分不清两种完全不同的来源：
            #   ① 用户显式点了"停止"（意图：这一轮连同它的子任务都停）；
            #   ② 回合被 300s 硬超时截断 / 浏览器断流（意图**不是**"杀掉后台子任务"）。
            # 此前两者一律连带取消，于是"父回合超时 → 后台子 Agent 被杀 → 它的结论永远
            # 回不到对话"成了常态（`submit turn truncated by deadline; this session's
            # sub-agents were cancelled, so their conclusions will NOT reach the conversation`）。
            #
            # 现在：①由**网关**在显式停止路径上直接广播 `subagent:cancel`（它知道用户意图，
            # 见 internal/api 的 BroadcastSubagentSessionCancel）；②让子 Agent 继续跑完 ——
            # 它们本就跑在独立任务里，终态会落库、结果可由 read_subagent_result 取回。
            active = 0
            if session_id:
                try:
                    from app.subagent import registry as subagent_registry

                    active = len(subagent_registry.list_active(session_id))
                except Exception as exc:  # noqa: BLE001 - 诊断失败不影响取消语义
                    logger.debug("list_active on parent cancel failed: %s", str(exc)[:160])
            if active:
                logger.info(
                    "parent turn ended without explicit stop → %d background subagent run(s) keep "
                    "running; the gateway stops them only on an explicit user stop",
                    active,
                )
            raise
        except Exception as e:
            logger.error("Agent submit error: %s", e)
            yield f"data: {json.dumps({'type': 'error', 'content': str(e)})}\n\n"
            # 异常收尾 → Webhook agent.error
            if session_id:
                try:
                    from app.event_bus import emit_agent_error

                    await emit_agent_error(
                        session_id, task.user_id or "", task.tenant_id or "default",
                        error=str(e),
                        tokens_used=total_in + total_out,
                        duration_ms=int((time.monotonic() - started) * 1000),
                    )
                except Exception as wh_err:  # noqa: BLE001
                    logger.debug("agent webhook emit failed: %s", wh_err)
        finally:
            # 停掉 runtime 泵：客户端断开时取消会经它传播到 agent 循环
            pump.cancel()
            try:
                await pump
            except asyncio.CancelledError:
                pass
            except Exception as pump_err:  # noqa: BLE001
                logger.debug("runtime pump cleanup failed: %s", pump_err)
            # 先注销 Redis 归属映射（仅当仍属于本次 run_token），再清进程内注册表
            if run_lease is not None:
                await run_lease.stop()
            if session_id:
                _ACTIVE_RUNTIMES.pop(session_id, None)

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


async def agent_approval(
    request: Request,
) -> Any:
    """工具确认端点：解决 agent 循环中等待用户确认的工具调用（S 安全修复）。"""
    body = await request.json()
    session_id = body.get("session_id", "")
    tool_call_id = body.get("tool_call_id", "")
    approved = bool(body.get("approved", False))
    reason = body.get("reason", "")
    entry = _ACTIVE_RUNTIMES.get(session_id)
    if entry is None:
        # 本地没有该 run：查 Redis 归属，区分「run 在别的实例」与「没有活动 run」。
        # 前者此前只得到含糊的 "no active agent"，前端/运维无法判断该不该重试。
        from app.run_registry import owner_of

        owner = await owner_of(_redis, session_id)
        if owner and owner.get("instance_id"):
            logger.warning(
                "approval rejected: run owned by another instance (session=%s owner=%s self=%s)",
                session_id,
                owner.get("instance_id"),
                _get_instance_id(),
            )
            return {
                "ok": False,
                "error": "run owned by another engine instance",
                "owner_instance_id": owner.get("instance_id"),
            }
        return {"ok": False, "error": "no active agent for this session"}

    # S 安全修复：校验来电者是否为会话 owner，防止他人代批/拒批危险工具。
    # 可信 user_id 由 Go 网关从已验证 JWT claims 写入 body(或 X-User-ID 头)，
    # 直连路径无该身份时不得放行他人。
    runtime, owner_uid, run_token = entry
    caller = request.headers.get("x-user-id", "") or body.get("user_id", "")
    if owner_uid and caller and owner_uid != caller:
        logger.warning(
            "approval rejected: caller %s != owner %s (session=%s)",
            caller,
            owner_uid,
            session_id,
        )
        return {"ok": False, "error": "not session owner"}
    # run token 校验（批次 4）：网关从 Redis 归属映射取出当前 run_token 注入，
    # 防止陈旧 run 的审批命中新 run（session 复用场景）。缺失时不强制
    # （兼容未启用归属映射的网关/直连调用）；带上且不匹配则一律拒绝。
    given_token = body.get("run_token") or request.headers.get("x-run-token", "")
    if given_token and given_token != run_token:
        logger.warning(
            "approval rejected: stale run token (session=%s given=%.8s expected=%.8s)",
            session_id,
            given_token,
            run_token,
        )
        return {"ok": False, "error": "stale run token"}
    resolved = await runtime.submit_approval(tool_call_id, approved, reason)
    return {"ok": resolved}


async def agent_answer(
    request: Request,
) -> Any:
    """结构化提问端点：把用户答案回填给等待中的 ask_user 调用。

    校验流程与审批端点完全一致 —— 二者都是「外部输入注入到正在运行的 agent 循环」
    的通道：会话归属、调用者身份、run token 缺一不可。
    """
    body = await request.json()
    session_id = body.get("session_id", "")
    tool_call_id = body.get("tool_call_id", "")
    answer = str(body.get("answer", ""))
    if not answer.strip():
        return {"ok": False, "error": "answer is required"}
    entry = _ACTIVE_RUNTIMES.get(session_id)
    if entry is None:
        from app.run_registry import owner_of

        owner = await owner_of(_redis, session_id)
        if owner and owner.get("instance_id"):
            logger.warning(
                "answer rejected: run owned by another instance (session=%s owner=%s self=%s)",
                session_id,
                owner.get("instance_id"),
                _get_instance_id(),
            )
            return {
                "ok": False,
                "error": "run owned by another engine instance",
                "owner_instance_id": owner.get("instance_id"),
            }
        return {"ok": False, "error": "no active agent for this session"}

    runtime, owner_uid, run_token = entry
    caller = request.headers.get("x-user-id", "") or body.get("user_id", "")
    if owner_uid and caller and owner_uid != caller:
        logger.warning(
            "answer rejected: caller %s != owner %s (session=%s)",
            caller,
            owner_uid,
            session_id,
        )
        return {"ok": False, "error": "not session owner"}
    given_token = body.get("run_token") or request.headers.get("x-run-token", "")
    if given_token and given_token != run_token:
        logger.warning(
            "answer rejected: stale run token (session=%s given=%.8s expected=%.8s)",
            session_id,
            given_token,
            run_token,
        )
        return {"ok": False, "error": "stale run token"}
    resolved = await runtime.submit_answer(tool_call_id, answer)
    return {"ok": resolved}


async def kb_build(
    request: Request,
    gateway: Any = Depends(get_gateway),
) -> Any:
    """文档 RAG 索引 — SSE 流式进度"""
    import base64
    import json

    from app.rag.builder import RAGBuilder

    body = await request.json()
    content_raw = body.get("content", "")
    try:
        content_bytes = base64.b64decode(content_raw)
    except Exception:
        content_bytes = content_raw.encode("utf-8")

    builder = RAGBuilder(llm_gateway=gateway)

    async def event_generator() -> AsyncIterator[Any]:
        try:
            async for event in builder.build_document(
                kb_id=body.get("kb_id", ""),
                doc_id=body.get("doc_id", ""),
                content=content_bytes,
                file_type=body.get("file_type", ""),
                filename=body.get("filename", ""),
                tenant_id=body.get("tenant_id", ""),
                vector_db=body.get("vector_db", "milvus"),
            ):
                yield f"data: {json.dumps(event, ensure_ascii=False)}\n\n"
        except Exception as e:
            yield f"data: {json.dumps({'type': 'error', 'message': str(e)})}\n\n"

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache"},
    )


async def kb_query(
    request: Request,
    gateway: Any = Depends(get_gateway),
) -> Any:
    """查询知识库"""
    from app.rag.builder import RAGBuilder

    body = await request.json()
    builder = RAGBuilder(llm_gateway=gateway)
    results = await builder.query(
        kb_id=body.get("kb_id", ""),
        query=body.get("query", ""),
        top_k=body.get("top_k", 5),
        threshold=body.get("threshold", 0.5),
        vector_db=body.get("vector_db", "milvus"),
    )
    return {"success": True, "results": results, "count": len(results)}


async def _run_retention_cleaner() -> None:
    """定期清理 task_idempotency 历史记录（A7/C1：长期运行防表膨胀）。

    保留期 TASK_IDEMPOTENCY_RETENTION_DAYS（默认 30 天），
    间隔 RETENTION_INTERVAL_HOURS（默认 6 小时）；清理失败只告警，不影响主链路。
    """
    import os as _os

    days = int(_os.getenv("TASK_IDEMPOTENCY_RETENTION_DAYS", "30") or 30)
    hours = int(_os.getenv("RETENTION_INTERVAL_HOURS", "6") or 6)
    from app.queue.idempotency import purge_older_than

    logger.info(
        "idempotency retention cleaner started (days=%d, interval=%dh)", days, hours
    )
    while True:
        try:
            await asyncio.sleep(hours * 3600)
            removed = await purge_older_than(days)
            if removed:
                logger.info(
                    "task_idempotency retention: removed %d rows (>%dd)", removed, days
                )
        except asyncio.CancelledError:
            logger.info("idempotency retention cleaner stopped")
            return
        except Exception as e:  # noqa: BLE001 - 清理失败不影响主链路
            logger.warning("idempotency retention failed: %s", e)


async def _run_queue_worker(redis: aioredis.Redis, gateway: Any = None) -> None:
    """后台队列消费者。"""
    global _queue_worker_instance
    from app.queue.worker import QueueWorker

    # 注意：子 Agent 的看门狗与取消订阅**不在这里**启动。它们由 lifespan 直接启动，
    # 否则"Redis 不可用"会连带导致子 Agent 完全无治理（看门狗本不需要 Redis）。
    # 见 app/main.py 启动序列第 6 步。

    worker = QueueWorker(
        redis=redis,
        concurrency=settings.queue_worker_concurrency,
        gateway=gateway,
        global_concurrency=settings.queue_worker_global_concurrency,
    )
    _queue_worker_instance = worker
    try:
        await worker.start()
    finally:
        # start() 内部会捕获 CancelledError 后正常返回，故不能只在 except 分支清理：
        # 这里兜底执行优雅停止（停 PEL reclaim loop、排空进行中任务）。
        # 关机路径已先调用 stop() 并清空 _queue_worker_instance，因此不会重复排空。
        if _queue_worker_instance is worker:
            _queue_worker_instance = None
            await worker.stop()
        # 停掉看门狗与取消订阅，并注销本实例持有的作业归属（P4-3）。
        # 注意这里必须**就地导入**：该函数（worker 常驻循环）在模块加载后才被调用，
        # 而此前这里引用的是一个从未导入的名字 —— 一旦走到这条 finally 就会 NameError，
        # 也就是说"关机时的收尾"其实一次都没成功执行过。
        from app.subagent import registry as subagent_registry

        await subagent_registry.stop()


def main() -> None:
    """主函数"""
    uvicorn.run(
        "app.main:create_app",
        factory=True,
        host=settings.http_host,
        port=settings.http_port,
        log_level="warning",  # 我们用 structlog，不需要 uvicorn 的日志
        access_log=False,
    )


def create_app() -> FastAPI:
    """创建 FastAPI 应用实例（供 uvicorn factory 模式使用）"""
    app = FastAPI(
        title="Chiron Python AI Engine",
        version="3.0.0",
        lifespan=lifespan,
    )
    _setup_middleware_early(app)
    _setup_routes(app)
    return app


def _setup_middleware_early(app: FastAPI) -> None:
    """注册中间件（在 app 创建时调用，lifespan 中补充 redis 依赖）"""
    from opentelemetry import trace
    from opentelemetry.propagate import extract

    from app.middleware.error_handler import ErrorHandlerMiddleware
    from app.middleware.metrics import MetricsMiddleware
    from app.middleware.privacy_middleware import PrivacyModeMiddleware
    from app.middleware.request_context import RequestContextMiddleware

    # Trace context propagation middleware
    @app.middleware("http")
    async def trace_context_middleware(request: Request, call_next: Any) -> Any:
        """从HTTP头提取trace context"""
        carrier = dict(request.headers)
        ctx = extract(carrier)

        tracer = trace.get_tracer(__name__)
        span = tracer.start_span(
            request.url.path,
            context=ctx,
            kind=trace.SpanKind.SERVER,
        )

        try:
            response = await call_next(request)
            span.set_status(trace.Status(trace.StatusCode.OK))
            return response
        except Exception as e:
            span.set_status(trace.Status(trace.StatusCode.ERROR, str(e)))
            raise
        finally:
            span.end()

    # 执行顺序(FastAPI 后注册先执行): PrivacyMode → RequestContext → Auth → Metrics → ErrorHandler → handler
    app.add_middleware(ErrorHandlerMiddleware)
    app.add_middleware(MetricsMiddleware)

    # ⚠️ 认证中间件必须挂上。此前它只存在于 _setup_middleware()——那是一条**从未被调用**
    # 的旧路径，create_app() 走的是本函数。后果是引擎 HTTP 面**完全没有认证**：
    # 任何能访问 8000 端口的人都能自报 ?user_id=&tenant_id= 冒充任意用户/租户，
    # 而 config.allow_direct_jwt 那层"唯一防线"也因此从未生效。
    #
    # 挂上不会中断正常链路：Go 网关对所有出站请求注入 X-Internal-Token
    # (internal/engine/python_client.go:289)，并剥离客户端伪造的同名头(:786)；
    # 容器探针走 /healthz、/readyz，均在 PUBLIC_PATHS 内。
    from app.config import settings as _settings
    from app.middleware.auth import AuthMiddleware

    app.add_middleware(
        AuthMiddleware,
        internal_token=_settings.internal_token,
        jwt_secret=_settings.jwt_secret,
    )
    app.add_middleware(RequestContextMiddleware)
    app.add_middleware(PrivacyModeMiddleware)

    # 混沌工程注入（默认关闭，见 settings.chaos_enabled）。
    #
    # 挂在**最后** = FastAPI 里**最先执行**：它要在认证之前就能生效 —— 注入模拟的是
    # 基础设施/整站故障，而不是某个用户的问题；实验的**创建**才需要 chaos:manage 权限
    # （网关把关）。关闭时中间件第一个判断就放行，几乎零开销。
    from app.chaos.injector import ChaosInjectionMiddleware

    app.add_middleware(ChaosInjectionMiddleware)


if __name__ == "__main__":
    main()
