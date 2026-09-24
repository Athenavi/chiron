# Python AI 引擎配置
from pathlib import Path

from pydantic import ConfigDict, model_validator
from pydantic_settings import BaseSettings


def _find_env_file() -> str:
    """从引擎代码位置向上探测项目根 .env（引擎可能以任意 cwd 启动）。

    修复：run.py 以 python-engine/ 为 cwd 启动引擎，pydantic 默认读 cwd 的
    .env（不存在）导致 LLM_API_KEY 等全部为空 → provider 未注册。
    这里从 app/config.py 向上找到项目根（含 run.py 的目录）的 .env。
    """
    cur = Path(__file__).resolve().parent  # python-engine/app
    for p in (cur.parent, cur.parent.parent, cur.parent.parent.parent):
        env = p / ".env"
        if env.is_file():
            return str(env)
    return ""


class Settings(BaseSettings):
    """Python AI 引擎配置，支持环境变量覆盖"""

    # ── HTTP Server ──
    http_port: int = 8000
    # 默认仅绑回环地址，避免误暴露到外网（生产需经反向代理/网关）
    http_host: str = "127.0.0.1"

    # ── Redis ──
    redis_url: str = ""
    redis_max_connections: int = 50
    # 统一键前缀（多环境共用同一 Redis 时隔离键空间）。
    # 必须与 Go 网关 REDIS_KEY_PREFIX 同值（约定含尾冒号，如 "dev:" / "prod:"）；默认空 = 存量兼容。
    redis_key_prefix: str = ""

    # ── 依赖门禁 ──
    # 默认 false：Redis 未配置或连接失败时直接拒绝启动（启动失败而非就绪降级），
    # 避免副本间会话/限流/队列不一致；仅单机开发显式 DEGRADED_MODE=true 才允许进程内降级。
    degraded_mode: bool = False

    # ── MCP 插件池（按节点开关）──
    # 多实例部署时仅需要的实例启用（每实例对活跃用户各持有 MCP 连接，全开会 N×连接放大）；
    # 默认开 = 保持单实例现状。关闭的实例不建 MCP 连接，相关工具调用会报不可用。
    mcp_pool_enabled: bool = True
    # MCP 连接预算（多实例部署的连接放大源）：
    # 连接总量 ≈ 实例数 × 活跃用户数 × 每用户 server 数，故按实例设上限更安全。
    # 活跃用户超过上限时只服务「最近活跃的前 N 个」；共享连接达到上限时新用户不再建连。
    # 0 = 不限制。被跳过的用户计入 mcp_pool_rejected_total 指标（见 observability/metrics.py）。
    mcp_max_users_per_instance: int = 20
    mcp_max_connections_per_instance: int = 50
    # MCP owner 租约（B1a）：开启后每个活跃用户只由一个引擎实例持有 MCP 连接，
    # 其它实例只注册「代理工具」并经 Redis 通道转发调用 ⇒ 连接数从 实例数×用户数×server
    # 降为 用户数×server。默认关闭（保持单实例/现状行为）；开启需 Redis 可用。
    mcp_owner_lease_enabled: bool = False
    mcp_owner_lease_ttl: int = 90  # 租约 TTL（秒）；owner 每轮轮询（25s）续期
    mcp_bridge_timeout: float = 30.0  # 跨实例工具调用超时（秒）

    # ── PostgreSQL ──
    # 默认空：强制通过 .env / POSTGRES_DSN 环境变量提供，避免误用开发库
    postgres_dsn: str = ""
    # 连接池大小（水平扩展时注意总连接数不超过数据库上限）
    db_pool_min_size: int = 5
    db_pool_max_size: int = 20

    # ── Milvus ──
    milvus_address: str = ""
    milvus_collection: str = "knowledge_base"

    # ── LLM Provider API Keys ──
    anthropic_api_key: str = ""
    anthropic_base_url: str = ""
    openai_api_key: str = ""
    openai_base_url: str = ""
    deepseek_api_key: str = ""
    deepseek_base_url: str = "https://api.deepseek.com"

    # 出站 LLM HTTP 请求的 User-Agent。必须是"具名客户端"标识：部分网关（如 opencode.ai）
    # 前置 Cloudflare 反滥用规则，会直接拒绝 SDK/HTTP 库的默认 UA
    # （实测 Python urllib / Go http 默认 UA 访问 opencode.ai 返回 CF Error 1010 → 403）。
    llm_http_user_agent: str = "chiron/1.0"

    # 单次 LLM 请求的 HTTP 超时（秒）。**必须有**：上游"建连成功但一直不返回"时，
    # 没有超时的 await 会永不返回 —— 同步委派的子 Agent 会把父 turn 一起拖住，
    # 直到 Go 侧的回合超时才兜底（用户看到的就是"主 Agent 长期阻塞"）。
    # 只限制单次尝试；重试次数由 SDK 的 max_retries 决定。
    llm_http_timeout: float = 120.0

    # 独立 MCP broker 的地址：引擎对危险 MCP server **不直连**（连接守卫会拒），改由它持凭据
    # 执行 —— 凭据只存在于 broker 进程，引擎即使被注入也读不到（见 python-engine/mcp_broker.py）。
    mcp_broker_url: str = "http://127.0.0.1:8001"

    # 工作流入队失败时是否退回本进程执行（默认**否**）。
    # 本地执行不持久化：副本重启任务就没了，而接口已经返回 running —— 属于"看起来成功、
    # 实际丢失"。默认标记 queued_pending 等拉起；只有确实需要这份可用性兜底时才打开，
    # 且打开时会打点告警（降级必须可见）。
    workflow_local_fallback: bool = False

    # ── 服务提供商目录（与 Go 网关 internal/api/llm_providers.go 对齐）──
    # 由网关 /v1/internal/engine-config 下发 llm_provider_catalog；为空则回落
    # app/providers/catalog.py 的兜底目录（网关不可达时的单机降级）。
    llm_provider_catalog: list[dict] = []
    # 网关「系统设置」python 分类里的 provider 级覆盖（{provider}_base_url /
    # {provider}_api_key）。Settings 不为每个 provider 声明字段，故集中收集于此。
    provider_overrides: dict[str, str] = {}

    # ── 统一 LLM 配置（与 Go Gateway 共用变量名）──
    llm_provider: str = "openai"
    llm_api_key: str = ""
    llm_base_url: str = "https://api.deepseek.com"
    llm_model: str = "deepseek-v4-flash"

    # ── Agent 配置 ──
    max_turns: int = 10
    default_model: str = "claude-sonnet-4-20250514"
    default_max_tokens: int = 4096
    default_temperature: float = 0.1

    # ── RAG 配置 ──
    embedding_model: str = "text-embedding-3-small"
    embedding_dim: int = 1536  # 嵌入维度，可配置
    chunk_size: int = 1000
    chunk_overlap: int = 200
    default_top_k: int = 5
    default_threshold: float = 0.7
    # 默认向量数据库: milvus | pgvector
    vector_db_type: str = "milvus"
    pgvector_table: str = "knowledge_chunk_vectors"
    # 本地嵌入模型路径（BGE/Jina 等），为空则跳过本地嵌入、回退到 API
    local_embedding_model: str = ""

    # ── 记忆配置 ──
    short_term_ttl: int = 604800  # 7 天（秒）
    long_term_ttl: int = 0  # 0 = 永不过期
    # L2 档案卡（用户长期记忆）容量与整理参数
    memory_profile_max_items: int = (
        200  # 每用户条目软上限（超出按 置信度×新近度 淘汰 derived）
    )
    memory_archive_days: int = 180  # 超期未引用且低置信 → 归档
    memory_dedup_threshold: float = 0.95  # cosine 超过该阈值判定近重复 → 整理时合并
    memory_search_min_cosine: float = 0.30  # 语义检索召回下限
    # L3 近期对话摘要（语义检索层）
    memory_summary_top_k: int = 5  # 每回合召回的摘要条数
    memory_summary_retention_days: int = 90  # 超期未命中 → archived
    memory_summary_min_cosine: float = 0.45  # 摘要语义检索召回下限
    memory_recall_token_budget: int = 8000  # L2+L3 总注入预算（tokens）
    memory_consolidate_batch: int = 32  # 巩固攒批大小

    # ── LLM Gateway 缓存 ──
    cache_l1_capacity: int = 2048
    cache_l2_ttl: int = 3600
    semantic_cache_threshold: float = 0.95
    semantic_cache_prefix_dims: int = 64

    # ── 插件/内部端点鉴权 ──
    # 与 Go 网关 LLM_GATEWAY_KEY 一致；插件 reload 等内部端点校验 X-API-Key
    llm_gateway_key: str = ""

    # ── HTTP超时配置（统一）─
    # P0性能修复：集中管理HTTP客户端超时，避免散落各处的魔法数字
    http_timeout_default: float = 15.0    # 默认超时（普通API调用）
    http_timeout_long: float = 60.0       # 长任务超时（文件上传/复杂查询）
    http_timeout_critical: float = 30.0   # 关键操作超时（支付/安全相关）
    http_timeout_web: float = 15.0        # 网页抓取超时（DuckDuckGo等）

    # ── 限流 ──
    rate_limit_rpm: int = 60  # requests per minute per tenant
    rate_limit_rps: int = 10  # requests per second per tenant

    # ── 队列 ──
    queue_worker_concurrency: int = 10
    # 后台队列 worker 跨实例全局并发上限(所有引擎实例共享,防多实例下 10×N 洪峰)。
    # 默认与单实例 concurrency 相同:单实例不收紧,多实例共享该上限。
    # 0 = 关闭全局门控(仅每实例 concurrency)。
    queue_worker_global_concurrency: int = 10

    # ── JWT ──
    # 默认空：未显式配置时若存在 APP_SECRET，则由其派生（与 Go 网关 deriveSubsecret 一致），
    # 保证引擎签发的 token 与 Go 网关共享同一密钥
    jwt_secret: str = ""

    # ── 部署级主密钥（与 Go 网关共享；.env 的 APP_SECRET 经 extra="ignore" 之外需显式声明才能读取）──
    app_secret: str = ""

    # ── Go 网关内部互信 token ──
    # 与 Go 网关共享，Python 仅在 X-Internal-Token 匹配时才接受
    # 网关透传的 ?tenant_id= / ?user_id= query 身份（防直连绕过）
    internal_token: str = ""

    # ── 直连引擎的 Bearer JWT 旁路（安全开关）──
    # false（默认）：引擎拒绝 Bearer 直连，只接受带 X-Internal-Token 的网关代理请求。
    # true：允许持任意合法 JWT 的调用方绕过网关直连引擎并自报 tenant_id ——
    #       会跳过网关的限流与审计，仅限本地调试/直连工具使用。
    allow_direct_jwt: bool = False

    # ── 混沌工程（默认关闭）──
    # false（默认）：注入中间件完全旁路，连 Redis 都不读。
    # true：按 ent_chaos_experiments 里的活跃实验，对引擎请求路径施加 latency / error。
    #
    # ⚠️ 这是**故意制造故障**的开关：只应在预发/演练环境开启。开启后任何持有
    # chaos:manage 权限的调用方都能让引擎按要求失败或变慢（网关侧的同名中间件另有
    # CHAOS_ENABLED 开关）。详见 docs/deployment-multi-instance.md 的混沌工程一节。
    chaos_enabled: bool = False

    # ── Go 网关内部配置下发端点（引擎启动时拉取后台「系统设置」中的 python 分类配置）──
    gateway_internal_url: str = "http://127.0.0.1:8080"

    # ── 可观测性 ──
    log_level: str = "INFO"
    otel_endpoint: str = ""  # e.g. "http://otel-collector:4317"

    # ── 实例标识（K8s 注入）──
    pod_name: str = ""
    instance_id: str = ""
    # 引擎对外可达地址（批 E1 动态发现）：如 http://engine-0:8000;
    # 为空则引擎不向 Redis 注册,网关回退静态 PYTHON_ENGINE_ADDRESS。
    engine_advertise_url: str = ""

    # extra="ignore"：项目根 .env 混有 Go 网关变量（PORT/CORS_ORIGINS 等），
    # Python 引擎只取自己声明的字段，其余忽略
    model_config = ConfigDict(
        env_prefix="", case_sensitive=False, extra="ignore", env_file=_find_env_file()
    )

    @model_validator(mode="after")
    def _validate_security_defaults(self):
        """P0 安全 fail-fast：JWT secret 必须显式配置且非弱值。

        历史问题：默认 jwt_secret='dev-secret-change-in-production' 会导致
        任何知道该值的攻击者可伪造任意租户身份的 JWT。Go 端已对弱值黑名单拒绝，
        Python 端必须保持一致校验，否则一旦 Python 端口直连暴露即可被绕过。

        生产模式（APP_ENV=production 或 PYTHON_ENV=production）下额外禁止
        sslmode=disable；开发模式允许，便于本地 docker postgres。
        """
        import base64
        import hashlib
        import hmac
        import os

        is_prod = (
            os.getenv("APP_ENV", "").lower() == "production"
            or os.getenv("PYTHON_ENV", "").lower() == "production"
        )

        # JWT_SECRET 未显式配置时，由 APP_SECRET 派生（与 Go 网关 deriveSubsecret 完全一致：
        # HMAC-SHA256(APP_SECRET, "chiron-jwt") → base64url 无 padding）。
        # 这样「仅配置 APP_SECRET」的部署模型下引擎也能启动，且与网关共享签名密钥。
        if not self.jwt_secret and self.app_secret:
            self.jwt_secret = (
                base64.urlsafe_b64encode(
                    hmac.new(
                        self.app_secret.encode("utf-8"), b"chiron-jwt", hashlib.sha256
                    ).digest()
                )
                .decode("ascii")
                .rstrip("=")
            )

        # INTERNAL_TOKEN 未显式配置时，由 APP_SECRET 派生（与 Go 网关 deriveSubsecret
        # 一致：HMAC-SHA256(APP_SECRET, "chiron-internal") → base64url 无 padding）。
        # Go 网关 ForwardRequest 注入的正是该派生值；不派生则引擎 internal_token 为空，
        # 需内部 token 的端点（如 /v1/admin/api-keys）恒 401，曾致登录后管理探测被前端判为会话失效。
        if not self.internal_token and self.app_secret:
            self.internal_token = (
                base64.urlsafe_b64encode(
                    hmac.new(
                        self.app_secret.encode("utf-8"),
                        b"chiron-internal",
                        hashlib.sha256,
                    ).digest()
                )
                .decode("ascii")
                .rstrip("=")
            )

        WEAK_SECRETS = {  # noqa: N806 — 函数内阈值集合，沿用模块级大写常量惯例
            "",
            "dev-secret-change-in-production",
            "dev-secret-change-in-production-12345678",
            "changeme",
            "change-me",
            "secret",
            "test-secret",
        }
        if not self.jwt_secret or self.jwt_secret in WEAK_SECRETS:
            raise ValueError(
                "JWT_SECRET must be set to a strong value (>=32 chars) "
                "via env or .env; weak/empty defaults are rejected"
            )
        if len(self.jwt_secret) < 32:
            raise ValueError(
                f"JWT_SECRET too short ({len(self.jwt_secret)} chars); "
                "must be at least 32 characters for HMAC-SHA256 security"
            )
        # PostgreSQL：开发/安装模式允许未配置（引擎降级启动，PG 相关功能不可用，
        # main.py 仅在 postgres_dsn 非空时初始化连接池）；生产模式仍强制。
        if not self.postgres_dsn and is_prod:
            raise ValueError(
                "POSTGRES_DSN must be explicitly set via env or .env (production)"
            )
        if is_prod and "sslmode=disable" in self.postgres_dsn:
            raise ValueError(
                "POSTGRES_DSN with sslmode=disable is forbidden in production; "
                "use sslmode=require or verify-full"
            )
        # DR R6(过渡):由 APP_SECRET 派生 JWT_SECRET/INTERNAL_TOKEN 是兼容旧部署的
        # 过渡路径;新部署应显式注入两者。完全移除派生前需先确认引擎启动链
        # (chiron-cli/启动器注入),避免本地仅 .env(APP_SECRET)的实例无法启动。
        import logging as _logging
        import os as _os

        if self.app_secret and not _os.getenv("JWT_SECRET") and self.jwt_secret:
            _logging.getLogger(__name__).warning(
                "JWT_SECRET derived from APP_SECRET (deprecated). "
                "DR R6: inject JWT_SECRET explicitly; derivation will be removed."
            )
        if self.app_secret and not _os.getenv("INTERNAL_TOKEN") and self.internal_token:
            _logging.getLogger(__name__).warning(
                "INTERNAL_TOKEN derived from APP_SECRET (deprecated). "
                "DR R6: inject INTERNAL_TOKEN explicitly; derivation will be removed."
            )
        return self

    @model_validator(mode="after")
    def _resolve_llm_fallback(self):
        """LLM_* 配置回退：当 OPENAI_* 为空时使用 LLM_*，且 LLM_MODEL 覆盖 default_model"""
        if self.llm_api_key and not self.openai_api_key:
            self.openai_api_key = self.llm_api_key
            # 仅当用户未显式配置 OPENAI_BASE_URL 时才用 LLM_BASE_URL 回退端点：
            # 用户显式设置的自定义 OpenAI 兼容端点（OPENAI_BASE_URL）不应被
            # LLM_BASE_URL（默认指向 DeepSeek）静默覆盖。
            if "openai_base_url" not in self.model_fields_set:
                self.openai_base_url = self.llm_base_url or self.openai_base_url
        # 仅当 llm_model 被显式设置（非默认值）时才覆盖 default_model，
        # 否则用户通过 DEFAULT_MODEL 环境变量设置的值会被静默忽略。
        if "llm_model" in self.model_fields_set:
            self.default_model = self.llm_model
        return self


settings = Settings()


def _load_gateway_config() -> dict:
    """从 Go 网关内部端点拉取后台「系统设置」的 python 分类配置。

    返回与 Settings 字段名一致的扁平 dict（敏感键已由网关用 APP_SECRET 解密）。
    网关不可达或未配置 internal_token 时返回空（fail-open，使用 env 默认值，不阻断启动）。
    """
    import json
    import logging
    import urllib.request

    if not settings.internal_token:
        return {}
    url = f"{settings.gateway_internal_url.rstrip('/')}/v1/internal/engine-config"
    try:
        req = urllib.request.Request(
            url, headers={"X-Internal-Token": settings.internal_token}
        )
        with urllib.request.urlopen(req, timeout=1) as resp:
            payload = json.loads(resp.read().decode())
        data = payload.get("data", payload) if isinstance(payload, dict) else {}
        return {k: v for k, v in data.items() if v is not None}
    except Exception as exc:  # noqa: BLE001 - 配置下发失败不阻断引擎启动
        logging.getLogger(__name__).warning(
            "load engine config from gateway failed: %s", exc
        )
        return {}


# 合并网关下发的配置：仅接受 Settings 已声明的字段，DB/env 之外不引入任意键。
_db_overrides = _load_gateway_config()
_allowed = set(Settings.model_fields.keys())
_merged = {k: v for k, v in _db_overrides.items() if k in _allowed}

# provider 级覆盖（{provider}_base_url / {provider}_api_key）：Settings 不为每个
# provider 逐个声明字段，故按后缀白名单单独收集到 provider_overrides，
# 由 app/providers/catalog.py 解析端点/key 时消费。
_provider_overrides = {
    k: v
    for k, v in _db_overrides.items()
    if k not in _allowed
    and k.endswith(("_base_url", "_api_key"))
    and isinstance(v, (str, int, float))
}
if _provider_overrides:
    _merged["provider_overrides"] = {
        k: str(v) for k, v in _provider_overrides.items() if str(v)
    }

if _merged:
    settings = settings.model_copy(update=_merged)
    import logging as _log

    _log.getLogger(__name__).info(
        "applied %d engine settings from gateway", len(_merged)
    )
