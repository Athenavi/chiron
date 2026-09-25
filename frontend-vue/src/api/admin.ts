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

export async function downloadBackup(): Promise<Blob> {
  const response = await api.post('/v1/admin/backup', null, { responseType: 'blob' })
  return response.data
}

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
