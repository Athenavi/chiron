# Prometheus 指标定义
from __future__ import annotations

import os

import psutil
from prometheus_client import Counter, Gauge, Histogram, Info

# ── HTTP 请求级 ──
HTTP_REQUESTS = Counter(
    "http_requests_total",
    "Total HTTP requests",
    ["method", "path", "status"],
)
HTTP_REQUEST_DURATION = Histogram(
    "http_request_duration_seconds",
    "HTTP request duration",
    ["method", "path"],
    buckets=[0.01, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0],
)

# ── LLM Gateway 级 ──
LLM_REQUESTS = Counter(
    "llm_requests_total",
    "Total LLM requests",
    ["provider", "model", "status"],
)
LLM_REQUEST_DURATION = Histogram(
    "llm_request_duration_seconds",
    "LLM request duration",
    ["provider", "model"],
    buckets=[0.1, 0.5, 1.0, 2.0, 5.0, 10.0, 30.0],
)
LLM_TOKENS = Counter(
    "llm_tokens_total",
    "Total LLM tokens consumed",
    ["provider", "model", "direction"],
)
LLM_CACHE_HITS = Counter(
    "llm_cache_hits_total",
    "LLM cache hits",
    ["level"],  # l1 / l2 / l3
)
LLM_CACHE_MISSES = Counter("llm_cache_misses_total", "LLM cache misses")
# M23：换 key 重试的结果分布。`retried` = 换了 key 再试（不是失败），`ok` = 换 key 后成功，
# `skipped` = 没换（错误不可重试 / 预算用尽 / 无 key 可换）。三个读数合起来回答
# "这套 key 环的健康度"：retried 持续上涨说明有 key 在反复失效。
LLM_KEY_RETRY = Counter(
    "llm_key_retry_total",
    "LLM key rotation retries (outcome: retried/ok/skipped)",
    ["provider", "outcome"],
)
LLM_CIRCUIT_STATE = Gauge(
    "llm_circuit_breaker_state",
    "Circuit breaker state (0=closed, 1=open)",
    ["provider"],
)

# ── 预算级 ──
TOKEN_BUDGET_USED = Gauge(
    "token_budget_used",
    "Token budget used this month",
    ["tenant_id"],
)
TOKEN_BUDGET_LIMIT = Gauge(
    "token_budget_limit",
    "Token budget monthly limit",
    ["tenant_id"],
)

# ── 队列级 ──
# ── MCP 插件连接池（B1）：多实例下的连接放大观测与预算 ──
MCP_POOL_CONNECTIONS = Gauge(
    "mcp_pool_connections", "MCP shared connections held by this engine instance"
)
MCP_POOL_USERS = Gauge(
    "mcp_pool_users", "Active users with MCP connections on this instance"
)
MCP_POOL_REJECTED = Counter(
    "mcp_pool_rejected_total",
    "MCP users/servers skipped because the instance connection budget was reached",
)

#: 限流降级计数（X1）：Redis 不可用时租户限流退回**进程内**实现 → 多副本下额度会放大 N 倍。
#: 这个数必须能被告警 —— 降级悄悄发生，就等于限流悄悄失效（"看起来有、实际没有"）。
#: 正常部署下它应恒为 0；一旦增长，说明 Redis 通路有问题，限流的全局保护已经名存实亡。
RATE_LIMIT_DEGRADED = Counter(
    "rate_limit_degraded_total",
    "Times tenant rate limiting fell back to the in-process limiter (Redis unavailable)",
)

#: 工作流入队失败转为"待执行"（X3）：入队失败不再静默本地跑（那种任务会随进程一起消失），
#: 而是标记 `queued_pending`、等启动时拉起。这个数应恒为 0 —— 一旦增长，说明 Redis/队列
#: 有过不可达，且有任务的完成时间被推迟到了下一次进程重启。
WORKFLOW_ENQUEUE_PENDING = Counter(
    "workflow_enqueue_pending_total",
    "Workflow instances that could not be enqueued and were marked queued_pending",
)

QUEUE_DEPTH = Gauge(
    "queue_depth",
    "Task queue depth",
    ["stream"],
)
QUEUE_PROCESSING_DURATION = Histogram(
    "queue_processing_duration_seconds",
    "Task processing duration",
    ["task_type"],
    buckets=[0.1, 0.5, 1.0, 5.0, 10.0, 30.0, 60.0],
)
QUEUE_DLQ_TOTAL = Counter(
    "queue_dlq_total",
    "Total tasks moved to dead letter queue",
    ["task_type"],
)
QUEUE_RETRY_TOTAL = Counter(
    "queue_retry_total",
    "Total tasks re-queued for retry",
    ["task_type"],
)

# ── 子 Agent 作业级（P1-3：状态转移的观测面）──
#
# 为什么需要：子 Agent 的终态写入点不止一处（正常收尾 / 取消收尾 / 超时定时器 /
# 僵尸收口器），历史上多次出现"某条路径忘了写终态"——DB 里留下 status='running'，
# 前端永久显示"运行中"，而**没有任何指标能反映它**（只能靠人去看 DB）。
# 这几项把"状态机是否收敛"变成可告警信号：
#
#   started - terminal{所有 status} ≈ 仍在运行的 run 数（本实例视角）
#
# 这些数只在**权威写入点**（subagent_runs 真写成功）递增，因此指标不会说谎：
# 指标涨了而 DB 没变，就说明持久化本身出了问题（见 SUBAGENT_PERSIST_FAILED）。
SUBAGENT_RUN_STARTED = Counter(
    "subagent_run_started_total",
    "Subagent runs that entered 'running' (counted only when the DB row was written)",
)
SUBAGENT_RUN_TERMINAL = Counter(
    "subagent_run_terminal_total",
    "Subagent runs that reached a terminal state",
    ["status"],  # completed / failed / cancelled / lost
)
SUBAGENT_RUN_UNFINALIZED = Gauge(
    "subagent_runs_unfinalized",
    "Runs this instance started but has not finalized (persistent growth means a "
    "finalize path is missing; 本项即 P1-3「状态转移的观测面」，定义见本文件上方同名小节)",
)
SUBAGENT_LINE_GATE = Counter(
    "subagent_line_gate_total",
    "Run-line budget gate trips (M15b): line_tokens = 累计到上限, line_paused = 被人按了暂停",
    ["axis"],
)
SUBAGENT_LINE_PAUSE = Counter(
    "subagent_line_pause_total",
    "M15c 线暂停与提额：paused = 暂停并挂出待办, paused_no_ticket = 暂停了但待办没建成（用户看不见）, "
    "raised = 人提了额并解除暂停, raise_failed = 提额写库失败",
    ["outcome"],
)
SUBAGENT_LINE_SWEEP = Counter(
    "subagent_line_sweep_total",
    "M17 死实例预留清扫：scanned = 核对了持有者但没有人死, removed = 摘掉的预留条数, "
    "drained = 优雅退出时主动释放的条数, failed = 清扫本身失败（不阻断判定，预留仍会自然到期）",
    ["outcome"],
)
SUBAGENT_LINE_PROJECTION = Counter(
    "subagent_line_projection_total",
    "M18 在途预留的重述（shadow 默认只算不写）：off = 开关关掉, shadow = 算了没写, "
    "seeded = 这条 run 本来没有预留、这次写上, raised / lowered / flat = 相对上一条的方向, "
    "skipped = 没有样本（还没花钱）⇒ 一个字都不写, failed = 写 Redis 失败（沿用旧预留）",
    ["outcome"],  # 标签必须有界：与 SUBAGENT_LINE_SWEEP 同一理由
)
SUBAGENT_LINE_PROJECTION_TOKENS = Histogram(
    "subagent_line_projection_tokens",
    "M18 重述后那条预留的值（切换 SUBAGENT_LINE_PROJECTION=enforce 前要先看这一条的分布： "
    "它决定同一条线允许的并发在途量会不会被放大）",
    buckets=[1_000, 5_000, 20_000, 50_000, 100_000, 200_000, 500_000, 1_000_000, 2_000_000],
)
INTERACTION_CREATE_FAILED = Counter(
    "interaction_create_failed_total",
    "Pending-interaction tickets that could not be written —— 一次"
    "「本该等人、但人永远不会看到」的人工介入",
    ["reason"],  # unknown_kind / missing_keys / no_pool / storage_error / conflict_answered
)
SUBAGENT_PERSIST_FAILED = Counter(
    "subagent_persist_failed_total",
    "Subagent state persistence failures (run state may be lost permanently)",
    ["component", "op"],  # component: db / redis
)

# ── C1 批 3：run 恢复（resume / reconciler） ──
#
# `reconcile_total{outcome}`：巡检每处理一个僵尸 run 记一次 ——
#   revived（旧主已死 → 标记可续跑）/ abandoned（快照超冷窗口）/ skipped_alive（旧主还活着）
# `resumed_total{window}`：**用户重试**时真的从快照续跑的次数（热 / 冷窗口）
# `resume_turns_saved_total`：这些续跑**省下的回合数**（= 快照的 turn_index，即不必重跑的回合）
RUN_RECONCILE_TOTAL = Counter(
    "run_reconcile_total",
    "Zombie run reconciliation outcomes (per run handled)",
    ["outcome"],  # revived / abandoned / skipped_alive
)
RUN_RESUMED_TOTAL = Counter(
    "run_resumed_total",
    "Turns resumed from a run checkpoint (user retry)",
    ["window"],  # hot / cold
)
RUN_RESUME_TURNS_SAVED = Counter(
    "run_resume_turns_saved_total",
    "Turns skipped because the run resumed from a checkpoint instead of re-running",
)

# ── 实例级 ──
INSTANCE_ACTIVE_REQUESTS = Gauge(
    "instance_active_requests",
    "Active requests on this instance",
)
INSTANCE_UPTIME = Gauge(
    "instance_uptime_seconds",
    "Instance uptime in seconds",
)
ENGINE_INFO = Info("engine", "Python AI Engine info")

# ── 系统资源指标 ──
PROCESS_CPU_PERCENT = Gauge(
    "process_cpu_percent",
    "Current CPU usage percentage",
)
PROCESS_MEMORY_RSS = Gauge(
    "process_memory_rss_bytes",
    "Resident set size in bytes",
)
PROCESS_MEMORY_VMS = Gauge(
    "process_memory_vms_bytes",
    "Virtual memory size in bytes",
)
PROCESS_THREADS = Gauge(
    "process_threads",
    "Number of threads",
)


def record_process_metrics() -> None:
    """记录进程资源使用指标"""
    try:
        process = psutil.Process(os.getpid())
        PROCESS_CPU_PERCENT.set(process.cpu_percent(interval=None))
        mem_info = process.memory_info()
        PROCESS_MEMORY_RSS.set(mem_info.rss)
        PROCESS_MEMORY_VMS.set(mem_info.vms)
        PROCESS_THREADS.set(process.num_threads())
    except Exception:
        # 静默失败，避免影响主流程
        pass


def get_active_requests() -> int:
    """当前实例的在途 HTTP 请求数（由 MetricsMiddleware 维护）。"""
    try:
        return int(INSTANCE_ACTIVE_REQUESTS._value.get())
    except Exception:
        return 0


def get_process_resources() -> tuple[float, float]:
    """返回 (CPU 占用百分比, 常驻内存 MB)。

    ``cpu_percent(interval=None)`` 返回「自上次调用以来」的平均占用：首次调用因无基线
    返回 0.0，属预期行为。
    """
    try:
        process = psutil.Process(os.getpid())
        cpu = float(process.cpu_percent(interval=None))
        rss_mb = process.memory_info().rss / (1024 * 1024)
        return cpu, rss_mb
    except Exception:
        return 0.0, 0.0
