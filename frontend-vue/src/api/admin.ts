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

export async function updateStorage(config: { backend: string; [key: string]: unknown }): Promise<{ status?: string; warning?: string; previous?: string; current?: string }> {
  const { data } = await api.put('/v1/admin/storage', config)
  return data?.data ?? {}
}

export async function testStorage(config?: { backend: string; [key: string]: unknown }): Promise<{ status?: string; message?: string }> {
  const { data } = await api.post('/v1/admin/storage/test', config ?? {})
  return data?.data ?? {}
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

// ── 租户级支付配置（/v1/tenant/payments）──
// 每个租户一套渠道凭据；未单独配置时后端返回平台默认值（using_default=true）。

export async function getTenantPaymentConfig(): Promise<PaymentConfigResponse> {
  const { data } = await api.get('/v1/tenant/payments')
  return data?.data ?? { config: {}, channels: {}, callback_urls: {} }
}

export async function saveTenantPaymentConfig(config: Record<string, unknown>): Promise<PaymentConfigResponse> {
  const { data } = await api.put('/v1/tenant/payments', { config })
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
  /** 给人看的名称（用户姓名 / 租户名）；缺失时前端回退到 bucket */
  label?: string
  input_tokens: number
  output_tokens: number
  /** 用户余额扣减（credits）；免费额度内为 0 */
  credits_charged: number
  /** 企业成本（分）；引擎未提供时为 0 */
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

/** 按维度（day / model / user / tenant）读取用量构成。tenant 仅平台 owner 可用。 */
export async function getUsageCatalog(params: {
  groupBy: 'day' | 'model' | 'user' | 'tenant'
  from?: string
  to?: string
  /** admin = 平台后台（只允许租户级）；tenant = 租户后台（允许成员级） */
  scope?: 'admin' | 'tenant'
}): Promise<UsageCatalog> {
  const path = params.scope === 'tenant' ? '/v1/tenant/usage/catalog' : '/v1/admin/usage/catalog'
  const { data } = await api.get(path, {
    params: { group_by: params.groupBy, from: params.from, to: params.to },
  })
  return data?.data ?? {
    group_by: params.groupBy, from: '', to: '',
    buckets: [], total: { bucket: '', input_tokens: 0, output_tokens: 0, credits_charged: 0, cost_cents: 0, records: 0 },
    bucket_cnt: 0, note: '',
  }
}

// ── 新用户引导漏斗（验收「完成率可查」） ──

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

// ── 发票审核（管理端） ──

/** 与后端 invoice_requests.status 的三个取值一致（单向：pending → approved|rejected） */
export type InvoiceStatus = 'pending' | 'approved' | 'rejected'

export interface AdminInvoice {
  id: string
  payment_id: string
  title: string
  tax_no: string
  amount_cents: number
  currency: string
  status: InvoiceStatus
  note?: string
  review_note?: string
  reviewed_at?: string
  created_at?: string
}

/**
 * 列出开票申请。
 *
 * `status` 不传时后端默认只给待审 —— 审核队列的默认视图就该是"等待我处理的事"。
 * 要看全量需显式传 `'all'`。
 */
export async function listInvoices(status?: InvoiceStatus | 'all'): Promise<AdminInvoice[]> {
  const { data } = await api.get('/v1/admin/invoices', {
    params: status ? { status } : undefined,
  })
  return data?.data?.invoices ?? []
}

/** 审核一张申请。status 只接受 approved / rejected；重复审核后端返回 409。 */
export async function reviewInvoice(
  id: string,
  payload: { status: 'approved' | 'rejected'; review_note?: string },
): Promise<{ id: string; status: InvoiceStatus }> {
  const { data } = await api.post(`/v1/admin/invoices/${encodeURIComponent(id)}/review`, payload)
  return data?.data ?? { id, status: payload.status }
}

// ── 租户级发票审核（/v1/tenant/invoices）──
// 只看/只审本租户用户提交的申请；后端按 claims.tenant_id 过滤（越权一律 404）。

export async function listTenantInvoices(status?: InvoiceStatus | 'all'): Promise<AdminInvoice[]> {
  const { data } = await api.get('/v1/tenant/invoices', {
    params: status ? { status } : undefined,
  })
  return data?.data?.invoices ?? []
}

export async function reviewTenantInvoice(
  id: string,
  payload: { status: 'approved' | 'rejected'; review_note?: string },
): Promise<{ id: string; status: InvoiceStatus }> {
  const { data } = await api.post(`/v1/tenant/invoices/${encodeURIComponent(id)}/review`, payload)
  return data?.data ?? { id, status: payload.status }
}

// ── 发票导出（CSV）──
//
// 用 `responseType: 'blob'`：响应体是 CSV 文件而不是 JSON。
// 代价是错误响应体也变成 Blob，调用方必须用 `blobErrorPayload` 读回文本，
// 否则拿不到"记录太多，请缩小范围"这类唯一有用的提示。
//
// 时间范围与状态过滤必须与列表页一致：用户点了"导出"，
// 期望拿到的是**当前筛选条件下的全量**，而不是另一套口径。

/** 导出当前租户的发票申请（`/tenant/invoices` 页）。 */
export async function exportTenantInvoicesCsv(params: {
  status?: InvoiceStatus | 'all'
  from?: string
  to?: string
}): Promise<Blob> {
  const resp = await api.get('/v1/tenant/invoices.csv', {
    params: {
      status: params.status && params.status !== 'all' ? params.status : undefined,
      from: params.from,
      to: params.to,
    },
    responseType: 'blob',
  })
  return resp.data as Blob
}

/** 导出平台全量发票申请（`/admin/invoices` 页，仅 owner）。 */
export async function exportInvoicesCsv(params: {
  status?: InvoiceStatus | 'all'
  from?: string
  to?: string
}): Promise<Blob> {
  const resp = await api.get('/v1/admin/invoices.csv', {
    params: {
      status: params.status && params.status !== 'all' ? params.status : undefined,
      from: params.from,
      to: params.to,
    },
    responseType: 'blob',
  })
  return resp.data as Blob
}

/**
 * 发票导出的文件名。
 *
 * 纯 ASCII、不含冒号（Windows 非法字符）。不依赖后端 `Content-Disposition`：
 * 跨域场景下浏览器默认读不到该响应头，前端自己生成更可靠 ——
 * 与 `utils/transactionExport.ts` 的取舍相同。
 */
export function invoiceExportFileName(scope: 'tenant' | 'admin', now: Date = new Date()): string {
  const pad = (n: number) => String(n).padStart(2, '0')
  const stamp = `${now.getUTCFullYear()}${pad(now.getUTCMonth() + 1)}${pad(now.getUTCDate())}T${pad(now.getUTCHours())}${pad(now.getUTCMinutes())}`
  return `invoices_${scope}_${stamp}.csv`
}

// ── 套餐目录（管理端） ──

/**
 * 后台可见的套餐目录项。**含已下架**（enabled=false）——
 * 与用户侧 `GET /v1/billing/packages` 的区别正在于此：用户只看得到在卖的。
 */
export interface AdminPackage {
  id: string
  code: string
  name: string
  credits: number
  price_cents: number
  currency: string
  duration_days: number
  /** 空数组 = 不限制模型 */
  allowed_models: string[]
  enabled: boolean
  sort_order: number
}

/** 新建/更新套餐的请求体。`code` 只在新建时生效（后端拒绝改 code）。 */
export interface AdminPackageInput {
  code?: string
  name: string
  credits: number
  price_cents: number
  currency: string
  duration_days: number
  allowed_models: string[]
  sort_order: number
}

export async function listAdminPackages(): Promise<AdminPackage[]> {
  const { data } = await api.get('/v1/tenant/packages')
  return data?.data ?? []
}

/** 新建套餐。code 重复时后端返回 409。 */
export async function createAdminPackage(payload: AdminPackageInput): Promise<AdminPackage | null> {
  const { data } = await api.post('/v1/tenant/packages', payload)
  return data?.data ?? null
}

/** 更新套餐（code 不可改）。id 不存在时后端返回 404。 */
export async function updateAdminPackage(
  id: string,
  payload: AdminPackageInput,
): Promise<AdminPackage | null> {
  const { data } = await api.put(`/v1/tenant/packages/${encodeURIComponent(id)}`, payload)
  return data?.data ?? null
}

/**
 * 下架套餐。后端做的是软下架（enabled=false），不是物理删除：
 * 已售权益是快照不受影响，但历史订单要靠 code 反查套餐名。
 */
export async function disableAdminPackage(id: string): Promise<void> {
  await api.delete(`/v1/tenant/packages/${encodeURIComponent(id)}`)
}

/** 重新上架套餐。 */
export async function restoreAdminPackage(id: string): Promise<void> {
  await api.post(`/v1/tenant/packages/${encodeURIComponent(id)}/restore`)
}

/** 平台内置套餐模板（只读）：租户新建套餐时可一键预填。 */
export async function listPackageTemplates(): Promise<AdminPackage[]> {
  const { data } = await api.get('/v1/tenant/package-templates')
  return data?.data ?? []
}

// ── 租户站点品牌（标题 / 图标 / 描述）──

export interface TenantSiteConfig {
  title: string
  icon_url: string
  description: string
}

export async function getTenantSiteConfig(): Promise<{ config: TenantSiteConfig; using_default: boolean }> {
  const { data } = await api.get('/v1/tenant/site-config')
  return data?.data ?? {
    config: { title: '', icon_url: '', description: '' },
    using_default: true,
  }
}

export async function saveTenantSiteConfig(config: TenantSiteConfig): Promise<void> {
  await api.put('/v1/tenant/site-config', config)
}

// ── 租户退款 ──

export interface PaymentRefund {
  id: string
  payment_id: string
  tenant_id: number
  user_id: string
  channel: string
  amount_cents: number
  currency: string
  reason: string
  status: string
  operator_id?: string
  channel_refund_id?: string
  error_message?: string
  created_at: string
  updated_at: string
}

export async function listTenantRefunds(page = 1, pageSize = 20) {
  const { data } = await api.get('/v1/tenant/refunds', {
    params: { page, page_size: pageSize },
  })
  return (data?.data ?? { refunds: [], total: 0, page }) as {
    refunds: PaymentRefund[]
    total: number
    page: number
  }
}

export async function listPaymentRefunds(paymentId: string): Promise<PaymentRefund[]> {
  const { data } = await api.get(`/v1/tenant/payments/${encodeURIComponent(paymentId)}/refunds`)
  return (data?.data?.refunds ?? []) as PaymentRefund[]
}

export async function createRefund(
  paymentId: string,
  amountCents: number,
  reason: string,
): Promise<PaymentRefund> {
  const { data } = await api.post(
    `/v1/tenant/payments/${encodeURIComponent(paymentId)}/refunds`,
    { amount_cents: amountCents, reason },
  )
  return data?.data as PaymentRefund
}

// ── 租户邀请 ──

export interface TenantInvite {
  code: string
  tenant_id: string
  max_uses: number
  uses: number
  expires_at: string | null
  created_at: string
}

export async function listTenantInvites(): Promise<TenantInvite[]> {
  const { data } = await api.get('/v1/admin/tenant/members/invites')
  return (data?.data?.invites ?? []) as TenantInvite[]
}

export async function createTenantInvite(maxUses = 1, expiresInDays = 7): Promise<TenantInvite> {
  const { data } = await api.post('/v1/admin/tenant/members/invites', {
    max_uses: maxUses,
    expires_in_days: expiresInDays,
  })
  return data?.data as TenantInvite
}

export async function revokeTenantInvite(code: string): Promise<void> {
  await api.delete(`/v1/admin/tenant/members/invites/${encodeURIComponent(code)}`)
}

// ── 租户概览 ──

export interface TenantOverview {
  members: number
  month_tokens: number
  month_cost_cents: number
  domains: number
  verified_domains: number
  pending_invoices: number
  active_entitlements: number
}

export async function getTenantOverview(): Promise<TenantOverview> {
  const { data } = await api.get('/v1/tenant/overview')
  return data?.data as TenantOverview
}

// ── 站点图标上传 ──

export async function uploadSiteIcon(file: File): Promise<string> {
  const form = new FormData()
  form.append('file', file)
  // 不手动设置 Content-Type：让 axios 生成带 boundary 的 multipart 头。
  const { data } = await api.post('/v1/tenant/site-icon', form)
  return String(data?.data?.url || '')
}

// ── 平台套餐模板管理（/v1/admin/package-templates，仅 owner）──

export async function listAdminPackageTemplates(): Promise<AdminPackage[]> {
  const { data } = await api.get('/v1/admin/package-templates')
  return data?.data ?? []
}

export async function createAdminPackageTemplate(payload: AdminPackageInput): Promise<AdminPackage | null> {
  const { data } = await api.post('/v1/admin/package-templates', payload)
  return data?.data ?? null
}

export async function updateAdminPackageTemplate(
  id: string,
  payload: AdminPackageInput,
): Promise<AdminPackage | null> {
  const { data } = await api.put(`/v1/admin/package-templates/${encodeURIComponent(id)}`, payload)
  return data?.data ?? null
}

export async function disableAdminPackageTemplate(id: string): Promise<void> {
  await api.delete(`/v1/admin/package-templates/${encodeURIComponent(id)}`)
}

export async function restoreAdminPackageTemplate(id: string): Promise<void> {
  await api.post(`/v1/admin/package-templates/${encodeURIComponent(id)}/restore`)
}

// ── 租户身份能力通知（/v1/tenant/identity-notice）──

export interface IdentityNotice {
  enabled: boolean
  providers: number
  captcha: boolean
  sms: boolean
  fingerprint: string
}

/** 本租户是否启用了身份能力（SSO / 人机验证 / 短信）。失败返回 null（通知是增强信息）。 */
export async function getIdentityNotice(): Promise<IdentityNotice | null> {
  try {
    const { data } = await api.get('/v1/tenant/identity-notice')
    return data?.data ?? null
  } catch {
    return null
  }
}

// ── 租户配额管理（查看 → 申请 → 平台审批）──

/** 与后端 ent_quota_pools.resource_type 的四个白名单值一致。 */
export type QuotaResourceType = 'token' | 'credits' | 'concurrency' | 'storage_mb'
export type QuotaPeriod = 'daily' | 'monthly'

/**
 * 配额池 + 当期用量。
 *
 * `used_amount` 与强制侧同源（token→usage_events、credits→credit_transactions、
 * storage_mb→media_assets）。`usage_source` 为 `none` 表示**查询失败或没有用量概念**
 * （如 concurrency），不要把它当 0 展示成"用了 0%"。
 */
export interface QuotaPool {
  resource_type: QuotaResourceType
  period: QuotaPeriod
  total_amount: number
  used_amount: number
  usage_source: string
  /** total_amount <= 0 表示不限额；前端不能直接按 0 算百分比（会得 NaN）。 */
  unlimited: boolean
}

export type QuotaRequestStatus = 'pending' | 'approved' | 'rejected'

export interface QuotaRequest {
  id: string
  tenant_id: number
  resource_type: QuotaResourceType
  period: QuotaPeriod
  current_amount: number
  requested_amount: number
  reason?: string
  status: QuotaRequestStatus
  review_note?: string
  reviewed_at?: string
  created_at?: string
}

/** 读本租户的配额池与当期用量。 */
export async function listTenantQuota(): Promise<QuotaPool[]> {
  const { data } = await api.get('/v1/tenant/quota')
  return data?.data?.pools ?? []
}

/** 读本租户的配额申请历史（不传 status 时后端默认只给待审）。 */
export async function listTenantQuotaRequests(
  status?: QuotaRequestStatus | 'all',
): Promise<QuotaRequest[]> {
  const { data } = await api.get('/v1/tenant/quota/requests', {
    params: status ? { status } : undefined,
  })
  return data?.data?.requests ?? []
}

/**
 * 提交一笔配额调整申请。
 *
 * `requested_amount = 0` 表示"取消限制"（后端与执行侧同语义）。
 * 同一资源已有待审申请时后端返回 409（`quota_request_pending`）。
 */
export async function createQuotaRequest(payload: {
  resource_type: QuotaResourceType
  period: QuotaPeriod
  requested_amount: number
  reason?: string
}): Promise<QuotaRequest> {
  const { data } = await api.post('/v1/tenant/quota/requests', payload)
  return data?.data
}

// ── 平台侧配额审批（/v1/admin/quota/requests，仅 owner）──

/** 列出待审/已处理的配额申请（平台视图，跨租户）。 */
export async function listAdminQuotaRequests(
  status?: QuotaRequestStatus | 'all',
): Promise<QuotaRequest[]> {
  const { data } = await api.get('/v1/admin/quota/requests', {
    params: status ? { status } : undefined,
  })
  return data?.data?.requests ?? []
}

/** 审批一笔配额申请。通过时后端会同步写入配额池并让闸门立即生效。 */
export async function reviewQuotaRequest(
  id: string,
  payload: { status: 'approved' | 'rejected'; review_note?: string },
): Promise<{ id: string; status: QuotaRequestStatus }> {
  const { data } = await api.post(
    `/v1/admin/quota/requests/${encodeURIComponent(id)}/review`,
    payload,
  )
  return data?.data ?? { id, status: payload.status }
}
