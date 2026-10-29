import api from './index'

// ── Types ──

export interface AdminMetrics {
  concurrent_connections: number
  queue_backlog: number
  cache_hit_rate: number
  api_latency_p99: number
  [key: string]: unknown
}

export interface AdminUser {
  id: string
  email: string
  name: string
  role: string
  created_at: string
  updated_at: string
}

export interface SystemInfo {
  version: string
  uptime_seconds: number
  database: string
  redis: string
  [key: string]: unknown
}

export interface StorageConfig {
  backend: string
  config: Record<string, unknown>
}

export interface RedisInfo {
  mode: string
  stats: Record<string, unknown>
}

export interface QueueStats {
  stream_key: string
  task_queue_length: number
  vip_queue_length: number
  groups: number
  consumers: number
  lag: number
  throughput_qps: number
  active_requests: number
  waiting_tasks: QueueTask[]
}

export interface QueueTask {
  task_id: string
  user_id: string
  content: string
  queued_at: string
  position: number
  is_vip: boolean
}

export interface CacheStats {
  total_hit_rate: number
  total_requests: number
  total_hits: number
  total_misses: number
  l1_hits: number
  l2_hits: number
  l3_hits: number
  redis_hit_rate: number
  redis_keyspace_hits: number
  redis_keyspace_misses: number
  redis_memory_mb: number
  redis_max_memory_mb: number
  hot_queries: HotQuery[]
}

export interface HotQuery {
  query: string
  hits: number
  hit_rate: number
  avg_latency_ms: number
}

export interface PerformanceStats {
  gateway: {
    instances: number
    cpu_percent: number
    memory_mb: number
    goroutines: number
    connections: number
    redis_latency_ms: number
    db_latency_ms: number
    uptime_seconds: number
    version: string
  }
  python_engine: {
    pods: number
    cpu_percent: number
    memory_mb: number
    active_tasks: number
    avg_inference_ms: number
    redis_latency_ms: number
    uptime_seconds: number
    version: string
  }
  latency_distribution: Record<string, number>
  qps_trend: { time: string; qps: number }[]
}

export interface ApiKey {
  id: string
  provider: string
  key_preview: string
  status: 'active' | 'rate_limited' | 'circuit_open'
  weight: number
  failures: number
  last_used: string
  remark: string
}

/** 服务提供商目录分组（与 Go 网关 internal/api/llm_providers.go 的 category 一致） */
export type LlmProviderCategory = 'international' | 'china' | 'aggregator' | 'self_hosted'

/** 接入协议：openai = OpenAI 兼容，anthropic = Anthropic Messages */
export type LlmProviderKind = 'openai' | 'anthropic'

/**
 * 服务提供商目录项 + 运行时状态。
 * 目录为权威源的只读投影：base_url 是目录默认端点，
 * base_url_override 是管理端在 DB 里的覆盖值，effective_base_url 是最终生效值。
 */
export interface LlmProviderPreset {
  id: string
  label: string
  vendor: string
  category: LlmProviderCategory
  kind: LlmProviderKind
  base_url: string
  api_key_env: string
  api_key_prefix: string
  model_prefixes: string[]
  docs_url: string
  cost: number
  quality: number
  requires_key: boolean
  model_discovery: boolean
  configured: boolean
  key_count: number
  base_url_override: string
  effective_base_url: string
}

// ── Dashboard ──

export async function getMetrics(): Promise<AdminMetrics> {
  const { data } = await api.get('/v1/admin/metrics')
  return data.data
}

// ── Users ──

export async function listUsers(): Promise<AdminUser[]> {
  const { data } = await api.get('/v1/admin/users')
  return data.data?.users || []
}

export async function getUser(id: string): Promise<AdminUser> {
  const { data } = await api.get(`/v1/admin/users/${id}`)
  return data.data?.user
}

export async function updateUser(id: string, updates: Partial<AdminUser>): Promise<void> {
  await api.put(`/v1/admin/users/${id}`, updates)
}

export async function deleteUser(id: string): Promise<void> {
  await api.delete(`/v1/admin/users/${id}`)
}

// ── System ──

export async function getSystemInfo(): Promise<SystemInfo> {
  const { data } = await api.get('/v1/admin/system')
  return data.data
}

export async function triggerMaintenance(action: 'vacuum' | 'reindex' | 'analyze' | 'flush_cache'): Promise<void> {
  await api.post('/v1/admin/maintenance', { action })
}

// 注：`downloadBackup`（POST /v1/admin/backup）已随「容器内备份」整体移除 ——
// 企业部署用云数据库，备份由服务商承担（快照 / PITR）。

// ── Storage ──

export async function getStorage(): Promise<StorageConfig> {
  const { data } = await api.get('/v1/admin/storage')
  return data.data
}

export async function updateStorage(config: { backend: string; [key: string]: unknown }): Promise<void> {
  await api.put('/v1/admin/storage', config)
}

export async function testStorage(): Promise<{ success: boolean; message: string }> {
  const { data } = await api.post('/v1/admin/storage/test')
  return data.data
}

// ── Redis ──

export async function getRedis(): Promise<RedisInfo> {
  const { data } = await api.get('/v1/admin/redis')
  return data.data
}

export async function updateRedis(config: { mode: string; [key: string]: unknown }): Promise<void> {
  await api.put('/v1/admin/redis', config)
}

export async function testRedis(): Promise<{ success: boolean; message: string }> {
  const { data } = await api.post('/v1/admin/redis/test')
  return data.data
}

// ── Queue ──

export async function getQueueStats(): Promise<QueueStats> {
  const { data } = await api.get('/v1/admin/queue')
  return data.data
}

export async function flushQueue(): Promise<void> {
  await api.post('/v1/admin/queue/flush')
}

export async function pauseQueue(pause: boolean): Promise<void> {
  await api.post('/v1/admin/queue/pause', { pause })
}

// ── Cache ──

export async function getCacheStats(): Promise<CacheStats> {
  const { data } = await api.get('/v1/admin/cache/stats')
  return data.data
}

// ── Performance ──

export async function getPerformance(): Promise<PerformanceStats> {
  const { data } = await api.get('/v1/admin/performance')
  return data.data
}

// ── API Keys ──

export async function listApiKeys(): Promise<ApiKey[]> {
  const { data } = await api.get('/v1/admin/api-keys')
  return data.data?.keys || []
}

export async function addApiKey(key: { provider: string; key: string; remark?: string; base_url?: string }): Promise<void> {
  await api.post('/v1/admin/api-keys', key)
}

export async function updateApiKey(id: string, updates: Partial<ApiKey>): Promise<void> {
  await api.put(`/v1/admin/api-keys/${id}`, updates)
}

/**
 * M27：改一把 key 的**分配权重**（smooth WRR 的比例）。
 *
 * 与 status 分开是有意的：`weight = 0` 的语义是"不参与选择但保留状态"（灰度摘流量），
 * 而不是"停用这把 key"（那是 status 的事）。
 */
export async function updateApiKeyWeight(id: string, weight: number) {
  await api.put(`/v1/admin/api-keys/${id}/weight`, { weight })
}

export async function deleteApiKey(id: string): Promise<void> {
  await api.delete(`/v1/admin/api-keys/${id}`)
}

// ── 服务提供商目录 ──

/** 拉取服务提供商目录与各 provider 的配置状态（key 条数 / 生效端点）。 */
export async function listLlmProviders(): Promise<LlmProviderPreset[]> {
  const { data } = await api.get('/v1/admin/llm-providers')
  return data.data?.providers || []
}

/**
 * 保存 provider 的端点覆盖（写 system_settings python 分类的 {id}_base_url）。
 * baseUrl 传空串即删除覆盖、回落目录默认端点。
 */
export async function saveLlmProviderBaseURL(id: string, baseUrl: string): Promise<void> {
  await api.put(`/v1/admin/llm-providers/${encodeURIComponent(id)}`, { base_url: baseUrl })
}

// ── Settings ──

export async function saveSettings(category: string, config: Record<string, unknown>): Promise<void> {
  await api.put('/v1/admin/settings', { category, config })
}

/** 读取已持久化的某类系统设置（rate_limit/degradation/cache/api_key） */
export async function getSettings(category: string): Promise<Record<string, unknown>> {
  const { data } = await api.get(`/v1/admin/settings?category=${encodeURIComponent(category)}`)
  return data?.data?.config ?? {}
}

// ── 支付渠道配置（支付宝 / 微信支付 / PayPal）──

/** 单个渠道的可用性：字段齐全、客户端构造成功且未被管理员停用时 enabled=true */
export interface PaymentChannelStatus {
  enabled: boolean
  currency: string
  /** 尚未生效的缺失项（管理员可读；已停用的渠道不列） */
  missing?: string[]
}

export interface PaymentConfigResponse {
  /** 当前生效配置（DB 覆盖 env 的结果），键与后台表单字段一一对应 */
  config: Record<string, unknown>
  channels: Record<string, PaymentChannelStatus>
  /** 需在渠道后台登记的异步通知地址 */
  callback_urls: Record<string, string>
}

/** 读取生效的支付渠道配置与各渠道可用性 */
export async function getPaymentConfig(): Promise<PaymentConfigResponse> {
  const { data } = await api.get('/v1/admin/payments')
  return data?.data ?? { config: {}, channels: {}, callback_urls: {} }
}

/**
 * 保存支付渠道配置：服务端先校验（非法私钥等直接 400 且不落库），
 * 通过后加密入库并热重建渠道客户端（无需重启），返回生效后的配置。
 */
export async function savePaymentConfig(config: Record<string, unknown>): Promise<PaymentConfigResponse> {
  const { data } = await api.put('/v1/admin/payments', { config })
  return data?.data ?? { config: {}, channels: {}, callback_urls: {} }
}

// ── D1：自诊断（"配了但不生效"的可自答清单）──────────────────────────────

export type DiagnosticStatus = 'ok' | 'warn' | 'fail' | 'skipped'

export interface DiagnosticCheck {
  id: string
  title: string
  /** config / dependency / capability —— 前端据此分组 */
  category: string
  status: DiagnosticStatus
  /** host 观测到的**事实**（不是建议） */
  detail: string
  /** 该怎么办；ok 时为空 */
  remediation?: string
}

export interface DiagnosticsReport {
  generated_at: number
  status: DiagnosticStatus
  counts: Record<string, number>
  checks: DiagnosticCheck[]
}

/** 读取自诊断报告。 */
export async function getDiagnostics(): Promise<DiagnosticsReport> {
  const { data } = await api.get('/v1/admin/diagnostics')
  return data?.data ?? { generated_at: 0, status: 'skipped', counts: {}, checks: [] }
}

// ── D3：用量目录（"花在哪"的构成，区别于配额用量的"还剩多少"）─────────────

export interface UsageBucket {
  bucket: string
  input_tokens: number
  output_tokens: number
  cost_cents: number
  records: number
}

export interface UsageCatalog {
  group_by: string
  from: string
  to: string
  buckets: UsageBucket[]
  total: UsageBucket
  bucket_cnt: number
  note: string
}

/** 按维度（day / model / user）读取用量构成。 */
export async function getUsageCatalog(params: {
  groupBy: 'day' | 'model' | 'user'
  from?: string
  to?: string
}): Promise<UsageCatalog> {
  const { data } = await api.get('/v1/admin/usage/catalog', {
    params: { group_by: params.groupBy, from: params.from, to: params.to },
  })
  return data?.data ?? {
    group_by: params.groupBy, from: '', to: '',
    buckets: [], total: { bucket: '', input_tokens: 0, output_tokens: 0, cost_cents: 0, records: 0 },
    bucket_cnt: 0, note: '',
  }
}

// ── 新用户引导漏斗（P0-3 验收「完成率可查」） ──

export interface OnboardingFunnelStep {
  step: string
  completed: number
  skipped: number
  pending: number
  /** 0–1 的比例，分母是 entered（进入过引导的人数） */
  completion_rate: number
  skip_rate: number
}

export interface OnboardingFunnel {
  /** 进入过引导的人数 —— 唯一正确的分母（不是全站用户数） */
  entered: number
  finished: number
  finished_rate: number
  steps: OnboardingFunnelStep[]
  steps_order: string[]
}

export async function getOnboardingFunnel(): Promise<OnboardingFunnel> {
  const { data } = await api.get('/v1/admin/onboarding/funnel')
  return data?.data ?? { entered: 0, finished: 0, finished_rate: 0, steps: [], steps_order: [] }
}
