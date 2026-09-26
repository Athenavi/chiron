import axios from 'axios'
import { describeApiError } from '../utils/apiError'
import { toToolList, type ToolInfo } from '../utils/toolList'
import type { WorkstationType } from '../types/workstation'

import { t } from '../i18n'
const API_URL = import.meta.env.VITE_API_URL || ''

// 注意：不要在这里设置默认 Content-Type！
// - 对 JSON 对象 axios 的 transformRequest 会自动设置 application/json
// - 对 FormData，显式 Content-Type 会阻止浏览器自动附加 multipart boundary，
//   导致后端 ParseMultipartForm 报 "invalid form"（上传 400）
export const api = axios.create({
  baseURL: API_URL,
  timeout: 30000,
  // S 安全：鉴权凭 httpOnly cookie（由后端 SetTokenCookie 下发），JS 不可读，
  // 避免 XSS 偷取 localStorage 中的 token。所有请求自动携带同源 cookie。
  withCredentials: true,
})

// 工具确认（S 安全修复：三态栅栏“确认”态 — 前端确认卡片回调）
export async function submitApproval(params: {
  session_id: string
  tool_call_id: string
  approved: boolean
  reason?: string
}): Promise<boolean> {
  const { data } = await api.post('/v1/agent/approval', params)
  return !!(data?.data?.ok ?? data?.ok)
}

// 结构化提问：把用户答案回填给等待中的 ask_user 工具调用（Python 侧 /v1/agent/answer）
export async function submitAnswer(params: {
  session_id: string
  tool_call_id: string
  answer: string
}): Promise<boolean> {
  const { data } = await api.post('/v1/agent/answer', params)
  return !!(data?.data?.ok ?? data?.ok)
}

// ── 会话操作（重命名 / 置顶）──
export async function updateConversation(id: string, patch: { title?: string; pinned?: boolean; tag?: string }) {
  const { data } = await api.put(`/v1/conversations/${encodeURIComponent(id)}`, patch)
  return data?.data
}

// ── 工具授权模式（ask/auto/yolo）不再是服务端状态 ──
// 它是前端的实时状态，随每次提交经 `llm_config.tools_mode` 携带（见 ChatView 的
// buildLlmConfig），由引擎在任务开始时采用并用于工具裁决（python-engine/app/agent/guards.py）。
// 曾有的 `GET/POST /v1/mode`（状态存 Redis）与 `session:mode:*` 键已删除 ——
// 同一份状态散落三处时，任一处不一致就表现为"设置存了却不生效"。

// ── Agents（DB 驱动：CRUD + 运行会话） ──
export interface Agent {
  id: string
  name: string
  description?: string
  system_prompt?: string
  tools?: unknown[]
  llm_config?: Record<string, unknown>
  max_turns: number
  timeout_seconds: number
  enabled: boolean
  /** 工作台绑定：默认知识库（派发时用于 RAG 检索） */
  kb_id?: string
  /** 工作台绑定：技能名数组（派发时只启用这些技能） */
  skills?: string[]
  /** 工作台绑定：插件名（MCP server）数组（派发时只放这些插件提供的工具） */
  plugins?: string[]
  /** 工作台绑定：工作流 id 数组（派发时按选择顺序执行，多选即流水线） */
  workflows?: string[]
  created_at: string
  updated_at: string
}

export interface AgentSession {
  id: string
  agent_id?: string
  agent_name?: string
  task: string
  status: 'pending' | 'running' | 'completed' | 'failed'
  result?: string
  created_at: string
  updated_at: string
}

export async function listAgents(): Promise<Agent[]> {
  const { data } = await api.get('/v1/agents')
  return data?.data ?? []
}

export async function createAgent(body: Partial<Agent>): Promise<Agent> {
  const { data } = await api.post('/v1/agents', body)
  return data?.data
}

export async function updateAgent(id: string, body: Partial<Agent>): Promise<Agent> {
  const { data } = await api.put(`/v1/agents/${encodeURIComponent(id)}`, body)
  return data?.data
}

export async function deleteAgent(id: string): Promise<void> {
  await api.delete(`/v1/agents/${encodeURIComponent(id)}`)
}

export async function runAgent(id: string, task: string): Promise<AgentSession> {
  const { data } = await api.post(`/v1/agents/${encodeURIComponent(id)}/run`, { task })
  return data?.data
}

export async function listAgentSessions(): Promise<AgentSession[]> {
  const { data } = await api.get('/v1/agents/sessions')
  return data?.data ?? []
}

export async function getAgentSession(id: string): Promise<AgentSession> {
  const { data } = await api.get(`/v1/agents/sessions/${encodeURIComponent(id)}`)
  return data?.data
}

// ── 六大工作台统一入口（quick-execute = chat/submit 的语义别名）──
export interface QuickExecuteResult {
  success: boolean
  session_id?: string
  trace_id?: string
  output?: string
  error?: string
  metadata?: {
    task_id?: string
    duration_ms?: number
    subtasks_completed?: number
  }
}

/** 快捷执行：自然语言任务 → TaskRouter 跨工作台自动编排 */
export async function quickExecute(body: {
  message: string
  session_id?: string
  mode?: 'auto' | 'agent' | 'workflow'
}): Promise<QuickExecuteResult> {
  const { data } = await api.post('/v1/quick-execute', body)
  return data
}

/** 获取统一会话消息历史（含跨工作台共享上下文） */
export async function getChatSessionMessages(sessionId: string) {
  const { data } = await api.get(`/v1/chat/sessions/${encodeURIComponent(sessionId)}/messages`)
  return data
}

// ── 会话分享（chat.deepseek.com/share/{id} 风格）──
export interface ShareInfo {
  share_id: string
  created_at?: string
}

/** 创建分享（body 为选中的消息 id；已有活跃分享时幂等返回） */
export async function createShare(sessionId: string, messageIds: string[]): Promise<ShareInfo> {
  const { data } = await api.post(`/v1/conversations/${encodeURIComponent(sessionId)}/share`, { message_ids: messageIds })
  return data?.data
}

/** 查询会话的活跃分享（无则抛 404） */
export async function getActiveShare(sessionId: string): Promise<ShareInfo> {
  const { data } = await api.get(`/v1/conversations/${encodeURIComponent(sessionId)}/share`)
  return data?.data
}

/** 撤销分享（公开链接随即失效） */
export async function revokeShare(sessionId: string): Promise<void> {
  await api.delete(`/v1/conversations/${encodeURIComponent(sessionId)}/share`)
}

export interface SharedMessage {
  role: 'user' | 'assistant'
  content: string
  created_at: string
}

export interface PublicShare {
  id: string
  title: string
  created_at: string
  messages: SharedMessage[]
}

/** 公开分享读取（无鉴权；已撤销返回 410）。走 /v1/share/{id} 与 SPA /share/:id 路由分离（S 修复 路径冲突）。 */
export async function getPublicShare(shareId: string): Promise<PublicShare> {
  const { data } = await api.get(`/v1/share/${encodeURIComponent(shareId)}`)
  return data?.data
}

// 鉴权凭 httpOnly cookie 自动携带（withCredentials），无需请求拦截器设置 Authorization。

// 响应拦截器：处理错误
api.interceptors.response.use(
  (response) => response,
  (error) => {
    if (error.response?.status === 401) {
      // Cookie 过期/失效：清本地 user 态，跳转登录（后端 cookie 由 /v1/auth/logout 清除）
      localStorage.removeItem('user')
      window.dispatchEvent(new CustomEvent('api:error', {
        detail: { message: t('auth.session_expired_please_sign_in_again') }
      }))
      // 短延迟让 toast 显示后再跳转
      setTimeout(() => { window.location.href = '/login' }, 500)
    } else if (error.response?.status >= 500) {
      // 触发全局错误事件，App.vue 中的监听器会显示提示。
      // 文案走统一翻译：把后端给出的具体原因一并带出 —— 只显示"服务器错误"会让用户
      // 与运维都无从定位（充值接口的 500 就属于这种）。
      window.dispatchEvent(new CustomEvent('api:error', {
        detail: { message: describeApiError(error) }
      }))
    } else if (error.code === 'ECONNABORTED' || !error.response) {
      // 网络超时或无法连接
      window.dispatchEvent(new CustomEvent('api:error', {
        detail: { message: t('errors.network_connection_failed_please_check_your_network_and_retry') }
      }))
    }
    return Promise.reject(error)
  }
)

// P1-2 文件上传：调用后端 POST /v1/media/upload（multipart）
// 后端返回 { id, name, type, file_url, size }，这里归一化为 ChatAttachment 所需结构
//
// 大文件分流：/v1/media/upload 是单请求流式上传，服务端上限 50MB
//（internal/api/media_upload.go maxInlineUpload）。超过阈值改走 /v1/uploads 分片上传，
// 由服务端合并后落 media_assets，避免超大文件被拒。
const INLINE_UPLOAD_LIMIT = 50 * 1024 * 1024

export async function uploadFile(file: File): Promise<{
  id: string
  url: string
  name: string
  size: number
  mimeType: string
}> {
  if (file.size > INLINE_UPLOAD_LIMIT) {
    const { createChunkUpload } = await import('../utils/uploader')
    const handle = await createChunkUpload(file, { purpose: 'media' })
    const done = await handle.done
    const id = done.asset_id || done.upload_id
    return {
      id,
      url: done.file_url || `/v1/media/${id}/download`,
      name: file.name,
      size: done.size || file.size,
      mimeType: file.type,
    }
  }

  const form = new FormData()
  form.append('file', file)
  // S 修复：不要手动设 multipart Content-Type（会丢 boundary 致 "invalid form"），
  // 让浏览器/axios 自动生成带 boundary 的正确头。
  const resp = await api.post('/v1/media/upload', form)
  const d = resp.data?.asset || resp.data || {}
  const id = d.id || d.asset_id || String(Date.now())
  // 后端返回字段 file_url；兜底 /v1/media/{id}/download
  const url = d.file_url || d.url || d.download_url || `/v1/media/${id}/download`
  return {
    id,
    url,
    name: d.name || d.filename || file.name,
    size: Number(d.size) || file.size,
    // 后端不返回 mime_type，用客户端声明的 file.type 兜底
    mimeType: d.mime_type || d.contentType || file.type,
  }
}

// SSE 连接
// 安全说明：session_id 必须出现在 URL 查询参数中，因为 EventSource API 不支持自定义 header。
// 风险：
//   - session_id 会出现在浏览器历史、服务器访问日志、Referer 头中
//   - 后端有 session 所有权校验（events.go:115）防止越权订阅
//   - 未来可考虑迁移到 WebSocket（ws.go）以消除 URL 暴露
// 权衡：当前方案用 withCredentials 携带 httpOnly cookie 鉴权，比 JWT 在 URL 中更安全
//
// last-event-id 续传（服务端见 internal/broadcast/hub.go + internal/api/events.go）：
//   - 带 session_id 的会话事件会先写入 Redis 缓冲流（sse:events:{session}，MAXLEN 200 + 滑动 TTL），
//     SSE 帧带 id: 行，浏览器解析为 event.lastEventId；
//   - 断线补发有两条通道：
//     a) autoReconnect=true：onerror 不立即关闭，交给浏览器原生自动重连——重连请求自动携带
//        Last-Event-ID 头，服务端 XRANGE 补发缺口（覆盖单轮流式中断线抖动，前端无感续传）；
//     b) 跨轮/手动重建：经 initialLastEventId 传入上一连接的最后事件 id（由 onLastEventId 采集），
//        服务端同样按 last_event_id 补发。
export interface SSEConnectionOptions {
  /** 重建连接时携带的最后事件 id（服务端补发该 id 之后缓冲的事件） */
  initialLastEventId?: string
  /** 允许浏览器自动重连（重连自动带 Last-Event-ID）；默认 false = 断线立即报错关闭（兼容旧行为） */
  autoReconnect?: boolean
  /** 自动重连的最大连续失败次数，超过后回调 onError 并关闭；默认 3 */
  maxAutoReconnects?: number
  /** 收到带 id 的事件时回调最新 last event id（供调用方持久化以跨轮续传） */
  onLastEventId?: (id: string) => void
}

export function createSSEConnection(
  sessionId: string,
  onMessage: (data: unknown) => void,
  onError?: () => void,
  opts: SSEConnectionOptions = {},
) {
  const { initialLastEventId = '', autoReconnect = false, maxAutoReconnects = 3 } = opts
  const onLastEventId = opts.onLastEventId

  let url = `${API_URL}/events?session_id=${encodeURIComponent(sessionId)}`
  if (initialLastEventId) {
    url += `&last_event_id=${encodeURIComponent(initialLastEventId)}`
  }

  const eventSource = new EventSource(url, { withCredentials: true })

  // 连续失败计数：自动重连期间每次 onopen 归零
  let failedReconnects = 0

  eventSource.onopen = () => {
    failedReconnects = 0
  }

  eventSource.onmessage = (event) => {
    try {
      const data = JSON.parse(event.data)
      onMessage(data)
    } catch {
      // SSE 解析错误不阻断连接
    }
    // 服务端输出的 id: 行 → 浏览器解析为 event.lastEventId；记录最新值供跨轮重建续传
    if (event.lastEventId) {
      onLastEventId?.(event.lastEventId)
    }
  }

  eventSource.onerror = () => {
    // 自动重连：单轮流式进行中网络抖动/网关实例切换时，留给浏览器原生重连
    // （重连请求自动携带 Last-Event-ID，服务端从缓冲流补发缺口）；连续失败超限才判定真断线。
    if (autoReconnect && failedReconnects < maxAutoReconnects) {
      failedReconnects++
      return
    }
    onError?.()
    eventSource.close()
  }

  return eventSource
}

export default api


// ─────────────────────────────────────────────────────────────
// 六大工作台互联互通 · 市场与媒体签名（2026-08-22）
// ─────────────────────────────────────────────────────────────

// ── 市场：技能 / Agent / MCP 浏览与一键安装 ──
export type MarketType = 'skill' | 'agent' | 'mcp'

export interface MarketItem {
  id: string
  type: MarketType
  name: string
  version: string
  status: string
  manifest: Record<string, unknown>
  installed?: boolean
}

export async function listMarket(type: MarketType): Promise<MarketItem[]> {
  const resp = await api.get('/v1/market', { params: { type } })
  // 兼容 OK() 包装与裸形态：层级取错会静默变成空列表（与 listModels 同一类坑）
  const d = resp.data?.data ?? resp.data
  return Array.isArray(d?.items) ? d.items : []
}

export async function installMarket(type: MarketType, itemID: string): Promise<unknown> {
  const resp = await api.post(`/v1/market/${type}/${itemID}/install`)
  return resp.data
}

// ── 媒体签名 URL（短时效，防裸公开猜测）──
const signedMediaCache = new Map<string, { url: string; exp: number }>()

export async function signMediaUrl(id: string): Promise<string> {
  const resp = await api.post(`/v1/media/${id}/sign`)
  // 后端返回格式: { success: true, data: { url: "..." } }
  return resp.data?.data?.url || resp.data?.url || ''
}

/**
 * 解析媒体资源为可访问 URL：非 /media/ 前缀(绝对/签名/data:)直接返回；
 * /media/ 公开路径统一改走签名 URL（带 12 分钟本地缓存）。
 */
export async function resolveMediaUrl(asset: { id?: string; file_url?: string }): Promise<string> {
  const f = asset?.file_url || ''
  if (f && !f.startsWith('/media/')) return f
  if (!asset?.id) return f
  
  // 清除过期缓存
  const now = Date.now()
  for (const [key, value] of signedMediaCache.entries()) {
    if (value.exp <= now) {
      signedMediaCache.delete(key)
    }
  }
  
  const cached = signedMediaCache.get(asset.id)
  if (cached && cached.exp > Date.now()) {
    return cached.url
  }
  
  try {
    const url = await signMediaUrl(asset.id)
    signedMediaCache.set(asset.id, { url, exp: Date.now() + 12 * 60 * 1000 })
    return url
  } catch {
    return f
  }
}


// ── 模型路由 / 定时自动化（2026-08-23）──
export interface LlmModel {
  provider: string
  name: string
  display_name: string
  context_window: number
}

export async function listModels(): Promise<LlmModel[]> {
  const resp = await api.get('/v1/models')
  // 后端走 OK() 包装：{success, data:{models:[…]}}。此前这里取 `resp.data?.models`
  // **少了一层 .data** → 拿到 undefined → 静默返回空数组 → 对话页的模型下拉永远显示
  // "暂无数据"（而此前后端恰好也是空的，症状完全一致，所以这个 bug 一直被掩盖）。
  // 这里同时兼容「已包装 / 未包装」两种形态，避免再出现"路径取错 = 静默空列表"。
  const d = resp.data?.data ?? resp.data
  return Array.isArray(d?.models) ? d.models : []
}

export async function triggerCronJob(id: string): Promise<unknown> {
  const resp = await api.post(`/v1/admin/cron-jobs/${id}/trigger`)
  return resp.data
}

/** 生成 cron job 的 Webhook 触发 URL（POST，token 为鉴权） */
export function cronWebhookUrl(id: string, token: string): string {
  return `${location.origin}/v1/hooks/${id}?token=${encodeURIComponent(token)}`
}


// ── 模板市场 / 共享可见性（2026-08-23 批次2）──
export interface TemplateItem {
  id: string
  type: 'workflow' | 'agent' | 'skill'
  name: string
  description: string
  /** 模板内容（workflow 模板为 `{nodes, edges}`）—— 具体形状由各工作台自行窄化 */
  payload: Record<string, unknown>
}

export async function listTemplates(type?: 'workflow' | 'agent' | 'skill'): Promise<TemplateItem[]> {
  const resp = await api.get('/v1/templates', { params: type ? { type } : {} })
  // 兼容 OK() 包装与裸形态（同上：取错层级 = 静默空列表）
  const d = resp.data?.data ?? resp.data
  return Array.isArray(d?.templates) ? d.templates : []
}

/** 工作台资源的统一形态（首页"带着工作台能力开对话"用） */
export interface WorkbenchResource {
  id: string
  name: string
}

/** 三个接口的返回外壳不一致（data.knowledge_bases / data.skills / data），且字段命名未必统一：
 *  这里宽松映射，拿不到 id 的条目直接丢弃，避免首页因个别脏数据整块渲染失败。 */
function toWorkbenchResources(raw: unknown): WorkbenchResource[] {
  if (!Array.isArray(raw)) return []
  const out: WorkbenchResource[] = []
  for (const item of raw) {
    const record = item as Record<string, unknown> | null
    const id = typeof record?.id === 'string' && record.id
      ? record.id
      : typeof record?.kb_id === 'string' ? record.kb_id : ''
    if (!id) continue
    const name = typeof record?.name === 'string' && record.name ? record.name : id
    out.push({ id, name })
  }
  return out
}

export async function listKnowledgeBases(): Promise<WorkbenchResource[]> {
  const resp = await api.get('/v1/kb')
  return toWorkbenchResources(resp.data?.data?.knowledge_bases)
}

export async function listSkillResources(): Promise<WorkbenchResource[]> {
  const resp = await api.get('/v1/skills')
  return toWorkbenchResources(resp.data?.data?.skills)
}

export async function listWorkflows(): Promise<WorkbenchResource[]> {
  const resp = await api.get('/v1/graphs')
  return toWorkbenchResources(resp.data?.data)
}

/**
 * 插件（= MCP server）列表。用于"把插件带进对话"：选中后本次对话只放它提供的
 * 工具（见 python-engine/app/agent/runtime.py 的插件筛选）。
 */
export async function listPlugins(): Promise<WorkbenchResource[]> {
  const resp = await api.get('/v1/plugins')
  return toWorkbenchResources(resp.data?.data)
}

/** 能力注册中心的一条能力（GET /v1/capabilities 的 capabilities[] 元素） */
export interface Capability {
  capability_id: string
  name: string
  description: string
  /** 所属工作台（六大标识的唯一定义见 src/types/workstation.ts） */
  workstation_type: WorkstationType
  /** 能力类型：tool / service / agent / workflow 等 */
  capability_type: string
  tags?: string[]
  status?: string
  version?: string
  stats?: { call_count?: number; success_rate?: number; avg_duration_ms?: number }
}

/**
 * 能力发现（互通的"发现侧"）：六类工作台注册的核心能力。
 *
 * 后端一直挂着真实执行器（app/core/capabilities.py 的 preload_default_capabilities），
 * 但前端此前从未调用 —— 能力注册中心成了死资产。返回体是
 * `{success, count, capabilities: []}`，这里只取数组。
 */
export async function listCapabilities(workstation?: string): Promise<Capability[]> {
  const resp = await api.get('/v1/capabilities', {
    params: workstation ? { workstation } : undefined,
  })
  return Array.isArray(resp.data?.capabilities) ? resp.data.capabilities : []
}

/** 语义搜索能力（按关键词 / 标签 / 描述匹配并排序） */
export async function searchCapabilities(query: string, limit = 20): Promise<Capability[]> {
  const resp = await api.post('/v1/capabilities/search', { query, limit })
  return Array.isArray(resp.data?.capabilities) ? resp.data.capabilities : []
}

/**
 * 保存工作流图定义。graph_json 由前端构造（节点用 `node_type` + `config`，
 * 连线用 `source_id`/`target_id`），后端原样序列化落库；同 id 覆盖。
 */
export async function createGraph(body: {
  name: string
  graph_json: unknown
  user_id?: string
}): Promise<{ id: string; name: string }> {
  const resp = await api.post('/v1/graphs', body)
  return resp.data?.data
}

/**
 * 模板「使用」接口的返回。
 *
 * 后端可能直接返回 `{payload,…}`，也可能包一层 `{data:{payload,…}}` —— 调用侧
 * （`WorkflowView.useWorkflowTemplate`）两种都兼容，这里把两种形状都声明出来。
 */
export interface TemplateUseResult {
  name?: string
  payload?: { nodes?: unknown[]; edges?: unknown[] }
  data?: TemplateUseResult
}

export async function useTemplate(id: string): Promise<TemplateUseResult> {
  const resp = await api.post(`/v1/templates/${id}/use`)
  return resp.data
}

/** 引擎当前可用的工具（含插件 / MCP 注入的代理工具），带完整 parameters schema */
export async function listTools(): Promise<ToolInfo[]> {
  const resp = await api.get('/v1/tools')
  return toToolList(resp.data)
}

export async function setAgentVisibility(id: string, visibility: 'private' | 'tenant' | 'public'): Promise<unknown> {
  const resp = await api.put(`/v1/agents/${id}/visibility`, { visibility })
  return resp.data
}

export async function setKBVisibility(id: string, visibility: 'private' | 'tenant' | 'public'): Promise<unknown> {
  const resp = await api.put(`/v1/kb/${id}/visibility`, { visibility })
  return resp.data
}

// ── 支付渠道（可用渠道由后台「支付配置」决定）──

export interface PaymentChannel {
  id: string
  /** 结算币种：支付宝/微信为 CNY，PayPal 为 USD */
  currency: string
}

/**
 * 查询后台已配置启用的支付渠道。
 * 充值页据此只展示真正可用的渠道 —— 否则用户选中未配置的渠道，
 * 要等下单后才拿到 501。
 */
export async function listPaymentChannels(): Promise<PaymentChannel[]> {
  const resp = await api.get('/v1/billing/channels')
  // 兼容 OK() 包装与裸形态（层级取错会静默变成空列表）
  const d = resp.data?.data ?? resp.data
  return Array.isArray(d?.channels) ? d.channels : []
}

