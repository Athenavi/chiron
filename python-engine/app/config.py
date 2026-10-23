# Python AI 引擎配置
from pathlib import Path
from typing import Any, Self

from pydantic import model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# 单租户默认租户 ID（与 Go internal/db/seed.go 保持一致）。
# 放在 config 而不是某个 API 模块里：写入侧（api）与执行侧（queue worker）必须用**同一个**
# 租户归一化规则，否则向量按 A 打标、检索按 B 过滤，结果是"构建成功但一条也查不到"。
# 注意 tenant_id 列是**可空**的（迁移 0001），存量 NULL 行匹配不上任何租户条件 ——
# 所以下游一律写 `tenant_id or DEFAULT_TENANT_ID`，而不是假设它非空。
DEFAULT_TENANT_ID = "00000000-0000-0000-0000-000000000001"


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

    # ── 文件后端（批 B 片 4）──
    # `local`（默认）：agent 文件工具直接读写 per-user workspace 本地目录 —— 单机行为不变。
    # `filestore`：经网关 `/v1/internal/storage/*` 读写部署级 FileStore（local 共享卷 / S3），
    #   多副本下"写 A 副本、读 B 副本"不再失忆。网关地址复用既有的 `gateway_internal_url`
    #   （见下方"Go 网关内部配置下发端点"），不另设一份以免两处漂移。
    # 用**显式开关**而不是"猜部署形态"：后者不可靠且不可审计（与评审 02 §1 的同一结论）。
    backend_kind: str = "local"

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
    # asyncpg 的 statement cache 大小。**经 PgBouncer（pool_mode=transaction）时必须为 0**：
    # prepared statement 绑在 server 连接上，而 transaction 池会在事务之间把连接换给别的客户端，
    # 缓存下来的语句名到下一个事务就不存在了（症状：`prepared statement "..." does not exist`，
    # 且只在并发下偶发）。默认 0 = 对两种部署都安全；确认直连 PG 且想省一次 parse 时可调到 100。
    pg_statement_cache_size: int = 0

    # ── Milvus ──
    milvus_address: str = ""

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
    llm_provider_catalog: list[dict[str, Any]] = []
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
    #: M23：一次 LLM 请求内**最多换几把 key** 再试一次（0 = 关掉整个机制，默认 1）。
    #: 只覆盖"还没向调用方产出任何东西"的那一次调用（见 `app/gateway/key_retry.py` 的
    #: "提交点"）—— 流式响应一旦开始吐 chunk 就绝不重试，否则重复输出 + 重复计费。
    #: 上限刻意低：重试会放大上游压力（LiteLLM 把 `max_retries` 钉成 0 正是为此）。
    key_retry_max: int = 1
    #: 单批内**并发**执行的工具数上限（C3）。只对"读级且无交互语义"的调用生效
    #: （见 `runtime._split_tool_batch`）；写/删/外部类与审批始终串行。
    #: 设为 1 即等价于"完全串行"（回滚开关）。
    tool_concurrency_limit: int = 4

    # ── P2：LLM 安全评审（guardian）──
    #: 工具被判「需要用户确认」时，先用一个**独立、无工具、有界**的 LLM 会话评审，
    #: 决定是否可以免人工审批（对照 Reasonix guardian）。默认开启。
    #:
    #: 安全底线（见 app/agent/guardian.py，**不接受配置放宽**）：
    #: delete / external 级永不被放行（不可逆 / 对外可见的操作始终要人）；
    #: 参数级硬拦截（secret 参数、路径逃逸）在评审**之前**已执行。
    #: 评审失败 / 无模型时**不猜测**，回退人工审批。
    guardian_enabled: bool = True
    #: 评审用的模型；空 = 复用本轮的模型。
    guardian_model: str = ""
    default_model: str = "claude-sonnet-4-20250514"
    default_max_tokens: int = 4096
    default_temperature: float = 0.1

    # ── 生命周期 hooks（批 G）──
    #: 机制总开关。**默认关** —— 开启前零行为变化（方案 03 §3.2）。
    hooks_enabled: bool = False
    #: 是否允许**用户自定义** hook（部署级 hook 不受此开关限制）。
    #: SaaS 部署误开必须可见：启用时启动告警 + 审计（方案 03 §3.2）。
    hooks_allow_user_defined: bool = False
    #: 单个 hook 的硬超时（秒）。超时按"失败"处理但**不阻断主流程**（fire-and-forget）。
    hooks_timeout_seconds: int = 5
    #: **一个事件**上所有 hook 的累计预算（秒）。没有它时最坏情况是 `N × 单 hook 超时`
    #: 会按 hook 数线性拖慢每一个工具调用；预算耗尽后**不再执行**后续 hook 并留审计。
    #: 默认 10 s：单个 hook（常见情形）行为与从前完全一致。
    hooks_event_budget_seconds: int = 10
    #: 部署级声明文件（`hooks.json`）路径。**默认空 = 不读文件、不注册、不执行 = 零行为变化**。
    #: 只接受**部署级**声明（文件里的条目一律视为部署级）；格式与纪律见
    #: `docs/hook-protocol-design.md` §4.2。改动需重启生效（不做热加载）。
    hooks_config_path: str = ""
    #: `command` 形态（运维声明的本地命令）的**独立开关**。**默认关** —— 它新增一个
    #: **运维级**可执行面，是 §6「不扩大可执行面」的一处显式例外（`docs/hook-protocol-design.md` §4.3 (b)）。
    hooks_allow_commands: bool = False
    #: `command` 形态允许使用的可执行文件 allowlist（逗号分隔；**裸名**或**绝对路径**）。
    #: **默认空 = 一条也不批准（fail-closed）**。它**不复制**面向模型的那张白名单 ——
    #: 两者信任级不同（前者约束"模型挑的命令"，后者是"**运维**声明的命令"）。
    hooks_command_allowlist: str = ""
    #: `webhook` 形态（出站 POST 到运维声明的 URL）的**独立开关**。**默认关** ——
    #: 它是一次**数据出境**面（`docs/hook-protocol-design.md` §4.3 批次 3）。
    hooks_allow_webhooks: bool = False
    #: `webhook` 形态允许的目标 host 白名单（逗号分隔，**只比 host 名**、不分大小写；
    #: scheme / 端口 / IP 段由 `app/tools/ssrf.py` 的既有守卫管）。
    #: **默认空 = 一个目标也不批准（fail-closed）**。
    hooks_webhook_allowlist: str = ""
    #: **上下文注入**开关（批次 4）。**默认关 ⇒ 零行为变化**。开启后，hook 输出里的
    #: `additional_context`（**结构化字段**，不是 stdout）经脱敏 + 长度上限 + 信任声明后，
    #: 作为**独立 system 消息**注入（复用记忆注入的形态）—— 它改变"什么能影响模型输入"。
    hooks_allow_context_injection: bool = False

    # ── 部署级扩展（批 H）──
    #: 机制总开关。**默认关** —— 关闭时零行为变化（与批 G 的 hooks 同一约定）。
    #: 开启后从**部署目录**读 `extensions.json`（只读、不进用户上传通道），重启生效。
    deploy_extensions_enabled: bool = False
    #: 部署目录（清单文件名固定为其中的 `extensions.json`）；清单引用的模块
    #: 必须落在该目录内，越界一律拒绝（方案 03 §4）。
    deploy_extensions_dir: str = ""

    # ── run 恢复（C1 批 3）──
    #: 僵尸 run 巡检（reconciler）。默认**开**：它只修正"旧主已死"的 run 状态
    #: （`running` → `checkpointed`；快照超冷窗口 → `abandoned`），**不触发续跑**、
    #: 不改对话行为。巡检本身只做 DB 扫描 + Redis 锁检查，周期 60s + 每实例抖动。
    run_reconciler_enabled: bool = True
    #: 自动续跑（C1 批 3+）：reconciler 在**热窗口（1h）内**发现僵尸 run 时，直接用它接管并跑完。
    #: 默认**开**：热窗口内"用户无感"才成立（SSE 重放能补齐缺口）。冷窗口**不自动跑** ——
    #: 用户早已离开，替他烧 token 跑完是花钱买一个他可能不想要的答案（那种情况只标记可续跑，
    #: 等他自己重试）。成本由 run 自身的 `task_budget` 兜底。
    run_auto_resume_enabled: bool = True

    # ── S3：远端子 agent（跨实例委派）──
    #: **默认关**：跨实例执行会扩大信任边界（另一台机器的引擎要接受本机的委派请求）——
    #: 与 hooks / 部署级扩展同一约定：默认关 = 零行为变化，显式开启才生效。
    #: 开启后 `target="remote:<instance_id|auto>"` 才可用；远端不可达时**回退本实例**。
    remote_subagent_enabled: bool = False

    # ── 嵌入配置（记忆层复用：L3 摘要向量的语义召回与近重复判定）──
    embedding_model: str = "text-embedding-3-small"
    embedding_dim: int = 1536  # 嵌入维度，可配置
    # 本地嵌入模型路径（BGE/Jina 等），为空则跳过本地嵌入、回退到 API
    local_embedding_model: str = ""

    # ── 知识库配置（PageIndex / S4 独立服务）──
    #: 检索服务内网地址（compose 里是 http://pageindex-search:8100）
    knowledge_service_url: str = ""
    # 问答是分钟级（多轮工具循环），120s 会误杀长文档；索引不走 HTTP。
    knowledge_service_timeout: float = 600.0
    #
    # 以下 knowledge_selfhosted_* 现在由**索引服务侧**读取（引擎不再索引文档）；
    # 保留声明只为兼容既有部署变量的文档与透传。
    # 自托管（PageIndex local 模式）的索引根目录。**必须位于所有引擎副本可见的存储上**
    # （共享卷 / NFS / RWX PVC）：local 模式没有中心服务，索引文件就是唯一真相，
    # 放在 Pod 本地盘上会表现为"同一份文档时而能查、时而查不到"。
    knowledge_selfhosted_root: str = ""
    # 自托管模式使用的索引 / 问答模型（经 app/providers 的 provider 解析拿端点与 key）。
    # 索引模型用基础档即可（树结构由布局提取，模型只做摘要精炼）；问答模型越强检索越准。
    knowledge_selfhosted_index_model: str = ""
    knowledge_selfhosted_chat_model: str = ""
    # Cloud 模式的问答模型（索引与存储在 PageIndex Cloud，只有"在树上推理"这一步用我们的模型）
    knowledge_cloud_chat_model: str = ""

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
    # L3 摘要向量的后端：pg | milvus（pgvector/postgres 等写法归一到 pg）。
    # 默认 **pg**：向量落 `memory_summaries.embedding`（迁移 0019），PG 是本项目唯一恒在的
    # 持久存储 —— 把 Milvus 当默认等于"语义层默认是死的"（compose 里 Milvus 属
    # selfhosted-s3 profile，注释自陈"默认不启动"），这正是本轮修掉的缺陷。
    # 与 RAG 的 `vector_db_type` 是**两个口径**：那个决定知识库向量存哪，这个只管摘要向量
    # 存哪 / 怎么扫；不跟着翻那个默认值，是为了不把 RAG 的存储路由一起改掉。
    # 切换 = 改配置 + 重启（后台系统设置里没有这一项，不是热开关）。非法值退回 pg 并告警。
    memory_vector_backend: str = "pg"
    # L3 近期对话摘要（语义检索层）
    memory_summary_top_k: int = 5  # 每回合召回的摘要条数
    memory_summary_retention_days: int = 90  # 超期未命中 → archived
    memory_summary_min_cosine: float = 0.45  # 摘要语义检索召回下限
    memory_recall_token_budget: int = 8000  # L2+L3 总注入预算（tokens）
    memory_consolidate_batch: int = 32  # 巩固攒批大小
    # ↑ 这个值此前**没有任何引用**（全库只在这一行出现），而 `on_turn_complete` 每回合
    # 都入队一次巩固，等于把"攒批"设计写成了"每回合"。本轮接上判定：只有 turn_count
    # 越过 batch 的整数倍才入队，会话末尾另由 rollup 覆盖。

    # ── L3 摘要正文的生成方式（A3：生成式摘要）──
    # 抽取式（旧行为）只是把每条消息前 200 字拼起来，既不像"摘要"，也让近重复判定的
    # 输入几乎全是原文噪声。默认开：巩固的节拍已被上面收成"每 batch 回合一次 + 会话
    # 结束一次"，不像 C6 提炼那样随对话量线性增长，所以不必默认关。
    # 关掉即退回抽取式拼接（Consolidator 的既有分支），不影响写入与向量。
    memory_summariser_enabled: bool = True
    #: 摘要用的模型；空 = 复用 `default_model`（与 `guardian_model` 同一口径）。
    #: 后台任务不必跟着本轮的会话模型走：那条信息在这里拿不到（任务只带 session_id）。
    memory_summariser_model: str = ""
    #: 输出上限：摘要是"几行要点"，给多了就是在为噪声付费。
    memory_summariser_max_tokens: int = 700
    #: 输入上限：巩固窗口取的是会话**全部**消息，长会话必须截断，否则每次调用都把
    #: 整段历史重送一遍，成本随会话长度平方增长（回合数 × 每回合的消息数）。
    memory_summariser_max_input_chars: int = 12000

    # ── C5：整理调度（自动触发，三个条件**任一**满足即可）──
    # 默认开：整理本身是既有能力（`organize_now` 幂等），自动化的价值是"不用等人点"。
    # 阈值刻意保守 —— 每次整理会调 embedding（花钱），不该写成"每回合都整理"。
    memory_organize_auto: bool = True
    memory_organize_min_entries: int = 20  # 条目数阈值
    memory_organize_min_turns: int = 10  # 自上次整理以来的累积回合数阈值
    memory_organize_interval_seconds: int = 6 * 3600  # 距上次整理的时长阈值（秒）

    # ── C6：回合级 L2 自动提炼（**默认关**）──
    # 默认关的理由见 app/memory/distill.py：它是"每回合多一次 LLM 调用"，成本随对话量线性增长。
    # 开启时用量计入 run 的 `task_budget` tokens 轴（与 S6b 的 grader 同一条规矩）。
    memory_distill_enabled: bool = False
    memory_distill_max_items: int = 3  # 一次提炼最多入库几条候选
    memory_distill_confidence: int = 30  # 候选条目的置信度（低置信 ⇒ 先被整理淘汰）

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

    # ── 观测端点令牌（/metrics、/info）──
    # 与网关的 METRICS_TOKEN 同值：Prometheus 抓取时带 `Authorization: Bearer <它>`。
    # 未配置时引擎**拒绝**这两个端点（fail-closed）—— 引擎在 k8s 里是 headless
    # Service，集群内任何 Pod 都能直连 8000，靠"端口不外露"是部署假设而不是保证。
    metrics_token: str = ""

    # ── 独立 sandbox 服务（S5-(e)，见 vendor/设计-独立sandbox服务.md）──
    #
    # `service`（**默认**）：执行转发给独立服务（`app/backends/remote_exec.py`），把 agent
    # 生成的命令 / Python 代码从引擎进程挪到**受限容器**里跑（只读根、无 capability、
    # 无外网出口）。这是 P0-1 的根治层。
    # `local`：执行走引擎进程内的基线（`app/tools/sandbox.py`）—— 仅适用于单机开发。
    #
    # **服务不可用时失败而不是回退本地** —— 静默回退会让"隔离已生效"这个判断失真
    # （方案 04 §4）。启动时会探测服务健康：不可达且 `degraded_mode=false`（生产默认）
    # ⇒ **拒绝启动**；`degraded_mode=true`（单机开发）⇒ 告警并回退本地。
    sandbox_backend: str = "service"

    #: 是否暴露 API 文档（`/docs`、`/redoc`、`/openapi.json`）。默认**关闭** ——
    #: 这几个路径在 `app/middleware/auth.py` 的 `PUBLIC_PATHS` 里免认证，端口一旦被
    #: 反向代理误暴露，就等于公开整个 API 契约。本地调试设 `DOCS_ENABLED=true`。
    docs_enabled: bool = False
    #: `service` 时的服务地址（仅内网可达）。compose/k8s 内的服务同名，故给默认值；
    #: 裸机开发想用进程内执行请设 `sandbox_backend=local`。
    sandbox_service_url: str = "http://sandbox-service:8001"
    #: **租户白名单**（逗号分隔）。空 = **全部租户走服务**（默认启用）；
    #: 填了名单则只有名单内租户走服务，其余留在本地（灰度 / 豁免用）。
    sandbox_service_tenants: str = ""
    #: 引擎侧对沙箱服务的**并发上限**（0 = 不限，保持现行为）。
    #: 多副本时建议设置：N 个引擎副本各自无限并发会把 sandbox-service 打满，表现为
    #: 排队 / 拒绝（其 `/v1/internal/exec/stats` 的 `rejected_total` 会给出信号）。
    #: 取值参考 `scripts/bench_sandbox.py` 实测出的"每实例安全并发数"。
    sandbox_service_max_concurrency: int = 0

    #: read-before-write **严格模式**（**已拍板：保持默认关**，2026-10-09）。
    #:
    #: * 关（默认）：**兼容旧行为** —— 只拦"读过但已变化"的文件，未读过的文件放行；
    #: * 开：**已存在但从未读过**的文件也拒绝（要求先 `read_file`），对位 DSH 的
    #:   `dsh-fs-observation-policy`（它把 read-before-edit 当硬要求）。
    #:
    #: 不存在的路径**两种模式都放行**：没有可过期的内容，新建文件本就没有"读"可做。
    #:
    #: **为什么不默认开**：收紧会让既有写入流程多出重读步骤，而收益需要产品侧判断。
    #: 与 DSH 的两处**已知差异**（同批决定不做，证据见 `内部差距分析`）：
    #: ① DSH 把"读一个不存在的路径"当作**授权受保护的创建**（`writeIntent` 返回 `createIfAbsent`）；
    #: ② DSH 有**并发创建**保护（读完缺失路径后别人建了它 ⇒ 拒绝），且它把原子新鲜度检查
    #: 交给 provider。用例：`tests/test_fs_guard.py::TestStrictMode`。
    fs_require_observation: bool = False

    # ── 子 Agent 的「继续 / 分叉」（R5(b)，见 vendor/规划.md §3.5）──
    #
    # 默认**关**：它让模型能"带着某个子 Agent 已记录的步骤继续跑（或从某一步分叉）"，
    # 是要显式开启的新能力（§1.3：会改变行为的机制一律默认关）。
    # 关闭时 `resume_subagent` 仍注册（与 R1/R2 同款运行时开关），但对任何调用都返回
    # **明确的"未启用"错误**，且不写 `subagent_runs.resumed_from` / `resume_at_step`。
    subagent_resume_enabled: bool = False

    # ── 子 Agent 的宿主证据（host receipts，见 vendor/规划.md §3.5 的 R1）──    #
    # 默认开：委派的价值就在**可信度** —— 父模型看到的 `summary` 是子 Agent 的自述，而收据是
    # **宿主观测**（工作区差分 + 执行计数），追加在摘要之后。
    # 关掉它会退回"只信自述"的旧行为（成本：执行前后各拍一次**有界**工作区快照）。
    subagent_host_receipts: bool = True

    # ── 子 Agent 写路径仲裁（R2，见 vendor/规划.md §3.5）──
    #
    # 默认**关**：开启它会让"可写的子 Agent"按写路径排队（同路径串行、不同路径并行），
    # 这是行为变化，所以默认必须是"什么都不做"（§1.3）。
    # 关闭时 `get_scheduler()` 返回 None，SubAgentRunner 不做 acquire/release。
    #
    # 为什么需要它：`background=True` 的子 Agent 与父 turn **真并发**且共享同一工作区，
    # 两个可写的子 Agent 写同一文件会**静默**互相覆盖（后写者赢，先写者的产出消失）。
    subagent_write_arbitration: bool = False
    #: 会话级并发上限（0 = 内置默认 6；上限 32）。只读委派也占这个槽。
    subagent_max_concurrency: int = 0
    #: 会话内**可写**委派的上限（0 = 内置默认 3）。
    subagent_max_writers: int = 0

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
    model_config = SettingsConfigDict(
        env_prefix="", case_sensitive=False, extra="ignore", env_file=_find_env_file()
    )

    @model_validator(mode="after")
    def _validate_security_defaults(self) -> Self:
        """P0 安全 fail-fast：JWT secret 必须显式配置且非弱值。

        历史问题：默认 jwt_secret='dev-secret-change-in-production' 会导致
        任何知道该值的攻击者可伪造任意租户身份的 JWT。Go 端已对弱值黑名单拒绝，
        Python 端必须保持一致校验，否则一旦 Python 端口直连暴露即可被绕过。

        生产模式（见 app/envmode.py：CHIRON_ENV / APP_ENV / PYTHON_ENV 任一命中）
        下额外禁止 sslmode=disable；开发模式允许，便于本地 docker postgres。
        """
        import base64
        import hashlib
        import hmac

        # 生产判定走 app.envmode 的**唯一入口**（并集认 CHIRON_ENV / APP_ENV / PYTHON_ENV）。
        # 此前这里只看 APP_ENV/PYTHON_ENV，而 deps.verify_sandbox_root 只看 CHIRON_ENV ⇒
        # 运维只设其中一个时，另一半门禁静默失效（"配置错了却起得来"）。
        from app.envmode import is_production

        is_prod = is_production()

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
    def _resolve_llm_fallback(self) -> Self:
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


def _load_secret_files() -> None:
    """把 `${NAME}_FILE` 指向的文件内容注入对应环境变量（**必须在 Settings() 构造之前**）。

    企业部署的密钥通常不在 env 里，而是由 Vault Agent Injector / K8s Secret 卷挂载 /
    云 Secrets Manager / CSI driver 以**文件**投递。这里做通用桥接：设
    `APP_SECRET_FILE=/vault/secrets/app_secret` 即可，无需把密钥展开成 env。
    已显式设置的 env 变量优先（不覆盖）；文件缺失 / 不可读时静默跳过。
    """
    import os

    for name in (
        "APP_SECRET",
        "JWT_SECRET",
        "INTERNAL_TOKEN",
        "S3_ACCESS_KEY",
        "S3_SECRET_KEY",
        "LLM_API_KEY",
        "METRICS_TOKEN",
    ):
        if os.getenv(name):
            continue  # 显式 env 优先
        path = os.getenv(f"{name}_FILE")
        if not path:
            continue
        try:
            value = Path(path).read_text(encoding="utf-8").strip()
        except OSError:
            continue
        if value:
            os.environ[name] = value


_load_secret_files()

settings = Settings()


def _load_gateway_config() -> dict[str, Any]:
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
