import axios from 'axios'
import { describeApiError } from '../utils/apiError'
import { toToolList, type ToolInfo } from '../utils/toolList'
import { canAutoRetry, readRetryable, resolveResponseWaitMs, shouldRetryResponse } from '../utils/rateLimitBackoff'
import type { WorkstationType } from '../types/workstation'

import { t } from '../i18n'

export const API_URL = import.meta.env.VITE_API_URL || ''

/**
 * 登录 / SSO 拿到的 Bearer token。
 *
 * 同源请求本来靠 httpOnly cookie 认证；额外保留内存里的 Bearer 是为了 SSO 回跳
 * 与网关直连等场景 —— 后端 AuthMiddleware 同时支持 cookie 与 Bearer。
 * 刻意不落 localStorage：XSS 拿不到，登出后也不残留。
 */
let bearerToken = ''

/** 写入本次请求使用的 Bearer token（空串表示清除）。 */
export function setAuthToken(token: string) {
  bearerToken = token || ''
}

function requestToken(): string {
  return bearerToken
}

// 注意：不要在这里设置默认 Content-Type！
// - 对 JSON 对象 axios 的 transformRequest 会自动设置 application/json
// - 对 FormData，显式 Content-Type 会阻止浏览器自动附加 multipart boundary，
//   导致后端 ParseMultipartForm 报 "invalid form"（上传 400）
export const api = axios.create({
  baseURL: API_URL,
  timeout: 30000,
  // 同源请求靠 httpOnly cookie 认证
  withCredentials: true,
})

api.interceptors.request.use((config) => {
  // Bearer token 由 setAuthToken 注入；没有就不设头
  const token = requestToken()
  if (token) config.headers.set('Authorization', `Bearer ${token}`)
  return config
})

/**
 * 手动修复卡住的会话（网关 `POST /v1/agent/sessions/{id}/recover`）。
 *
 * 严格模式：只有**确实停滞**的 run 才会被收口 —— Redis 运行锁仍在、或该行最后更新
 * 距今不足 6 分钟时，后端返回 409（`session_busy`）并在 `params.retry_after_seconds`
 * 里给出"最坏还要等多久"。
 */
export async function recoverSession(sessionId: string) {
  return api.post(`/v1/agent/sessions/${encodeURIComponent(sessionId)}/recover`)
}

// 工具确认（S 安全修复：三态栅栏“确认”态 — 前端确认卡片回调）
export async function submitApproval(params: {
  session_id: string
  tool_call_id: string
  approved: boolean
  /**
   * C5：审批三态。省略时由 `approved` 推导 —— 因此旧调用方行为不变。
   * `edit` = “编辑后批准”，此时必须带 `arguments`。
   */
  decision?: 'approve' | 'reject' | 'edit'
  /**
   * C5：`decision=edit` 时必填 —— **编辑后的完整参数对象**（JSON 字符串，整份替换）。
   * 引擎会用它重跑工具分级：若动作级别升高（如 write→delete），则**不执行**并给出原因。
   */
  arguments?: string
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
  /** 显式拒绝回答：与"超时/没看见"是不同的答复，后端据此回填哨兵值给模型。 */
  declined?: boolean
}): Promise<boolean> {
  const { data } = await api.post('/v1/agent/answer', params)
  return !!(data?.data?.ok ?? data?.ok)
}

/**
 * 运行中补充指令（steer 通道）：把一段话送进**正在跑**的回合，不终止它。
 *
 * 与 `sendMessage` 的分工：那个是"打断当前回合（或新开一轮）并重新提问"，
 * 这个是"让模型接着当前工作，但改一下方向"。差别对用户是实打实的 ——
 * 长任务跑到一半发现要补充约束时，打断等于丢掉已经跑了十几分钟的那一轮。
 *
 * `session_id` 必填；`run_token` 由网关自己从 run 亲和路由里取（前端不该持有它）。
 * 拒绝是**抛错**而不是返回 false：网关把引擎的 `ok:false` 翻成了 4xx/429 等状态码
 * （`submit_handler.go` 的 `steerStatus`），调用方据此区分"重试有用"与"没用"。
 */
export async function steerRun(params: { session_id: string; text: string }): Promise<boolean> {
  const { data } = await api.post('/v1/agent/steer', params)
  return !!(data?.data?.ok ?? data?.ok ?? data?.success)
}

// ── 会话操作（重命名 / 置顶）──
export async function updateConversation(id: string, patch: { title?: string; pinned?: boolean; tag?: string; alias?: string }) {
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
  /**
   * 归属人。列表接口会同时返回本租户内别人共享（visibility='tenant'）的 Agent，
   * 靠这个字段区分「我的」与「团队共享的」—— 只有所有者能改/删（后端 update、
   * delete 都带 user_id 条件，非属主一律 403）。
   */
  user_id?: string
  /** 可见性：'private' 仅自己 / 'tenant' 本租户共享 */
  visibility?: 'private' | 'tenant'
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
  mode?: 'auto' | 'agent'
}): Promise<QuickExecuteResult> {
  const { data } = await api.post('/v1/quick-execute', body)
  return data
}

/** 获取统一会话消息历史（含跨工作台共享上下文） */
export async function getChatSessionMessages(sessionId: string) {
  const { data } = await api.get(`/v1/chat/sessions/${encodeURIComponent(sessionId)}/messages`)
  return data
}

/** 工具调用 output 的一段原文（列表接口只给预览，全文按 offset 分段取） */
export interface ToolOutputChunk {
  content: string
  /** 库中 output 的完整字节数，用于判断还剩多少未加载 */
  total_bytes: number
  /** 完整**字符（码点）**数，与 offset / next_offset 同口径 */
  total_chars: number
  offset: number
  /** 服务端实际采用的上限，可能小于请求值（服务端强制夹取） */
  limit: number
  /**
   * 下一段的起始偏移，**由服务端算好**。
   *
   * 不要自己用「已收内容的 `.length`」推进：服务端按 Unicode 码点切，
   * 而 JS 的 `string.length` 数 UTF-16 码元 —— 含 emoji 时相差一倍，
   * 据此计算会跳过若干字符。`next_offset >= total_chars` 即已读完。
   */
  next_offset: number
}

/**
 * 按需读取一条工具调用的完整 output。
 *
 * limit 不要传超过 64KB：服务端会静默夹取，传大了只是白填。
 */
export async function getToolCallOutput(
  sessionId: string,
  toolCallId: string,
  offset = 0,
  limit = 8192,
): Promise<ToolOutputChunk> {
  const { data } = await api.get(
    `/v1/chat/sessions/${encodeURIComponent(sessionId)}/tool-calls/${encodeURIComponent(toolCallId)}/output`,
    { params: { offset, limit } },
  )
  return data?.data
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
  async (error) => {
    const status: number | undefined = error.response?.status
    // 服务端显式结论（`APIResponse.retryable`）：有就听它的，没有（undefined）才回退到状态码。
    const retryable = readRetryable(error.response?.data?.retryable)

    // 重试决策的分层（退避公式）：
    //   `retryable === true`  → 服务端说重试有用（如 409 session_busy），就重试，不管状态码；
    //   `retryable === false` → 服务端说重试无用（如参数非法），就**不**重试；
    //   `retryable` 缺失       → 调用方自判，用旧的 429/503 判断（后向兼容）。
    // 无限重试只会把账号打进更深的限流，所以 3 次上限仍是硬约束。
    if (shouldRetryResponse(status, retryable)) {
      const config = error.config as (typeof error.config & { __rateLimitRetry?: number }) | undefined
      const attempt = (config?.__rateLimitRetry ?? 0) + 1
      if (config && canAutoRetry(attempt)) {
        config.__rateLimitRetry = attempt
        const waitMs = resolveResponseWaitMs(
          attempt,
          error.response?.headers?.['retry-after'],
          error.response?.data,
        )
        // 只第一次提示：退避可能长达几十秒，静默会让用户以为界面卡死；
        // 每次重试都弹则是在刷屏。
        if (attempt === 1) {
          window.dispatchEvent(new CustomEvent('api:error', {
            detail: { message: t('errors.server_busy_retrying_automatically_in_n_seconds', { n: Math.ceil(waitMs / 1000) }) },
          }))
        }
        await new Promise(resolve => setTimeout(resolve, waitMs))
        return api.request(config)
      }
      window.dispatchEvent(new CustomEvent('api:error', {
        detail: { message: t('errors.server_busy_please_try_again_later') },
      }))
      return Promise.reject(error)
    }

    if (status === 401) {
      // Cookie/token 过期：清本地登录态，跳转登录页
      localStorage.removeItem('user')
      setAuthToken('')
      window.dispatchEvent(new CustomEvent('api:error', {
        detail: { message: t('auth.session_expired_please_sign_in_again') }
      }))
      // 短延迟让 toast 显示后再跳转。
      // 登录页自身的 401 不再硬跳：window.location 重载会形成刷新循环
      // （登录页挂载时的公开配置请求若失败，会把页面无限刷成白屏）。
      if (window.location.pathname !== '/login') {
        setTimeout(() => {
          window.location.href = '/login'
        }, 500)
      }
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

// 文件上传：调用后端 POST /v1/media/upload（multipart）
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
//
// 为什么是 fetch + ReadableStream 而不是 EventSource：**EventSource 不能设置请求头**，
// 于是这条通道永远带不出 Authorization。打包桌面态的身份是 `POST /v1/auth/session` 换到的
// 本地 JWT（见 internal/api/auth_local_session.go），订阅 `/events` 不带它就是 401/403 ——
// 而 EventSource 的 onerror 在浏览器自动重连之前不会告诉我们原因，前端于是停在「思考中」：
// 既不报错，也不出字。现在 header 带 Bearer、非 2xx 立即失败（`loading` 一定能被清掉）、
// 断线按 Last-Event-ID 续传（网关 header 优先于 query，见 internal/api/events.go:49）。
//
// 遗留：session_id 仍在 URL 查询参数里（服务端有所有权校验，见 events.go）；它会出现在浏览器
// 历史与访问日志里，改 WebSocket 才能消除，本批次不动。
//
// last-event-id 续传（服务端见 internal/broadcast/hub.go + internal/api/events.go）：
//   - 带 session_id 的会话事件先写入 Redis 缓冲流（sse:events:{session}，MAXLEN 200 + 滑动 TTL），
//     SSE 帧带 id: 行；收到即记录，重连时放进 Last-Event-ID 头；
//   - 缺口补发由服务端 XRANGE 完成（hub.ReplayAfter），前端只负责带回"我最后看到哪一条"；
//   - 旧的"交给浏览器原生重连"没有了：fetch 流不会自动重连，retry 由下面自己实现，
//     所以 autoReconnect 的语义不变（连续失败上限到了才 onError）。
export interface SSEConnectionOptions {
  /** 重建连接时携带的最后事件 id（服务端补发该 id 之后缓冲的事件） */
  initialLastEventId?: string
  /** 断线后自动重连（重连携带 Last-Event-ID）；默认 false = 断线立即报错关闭（兼容旧行为） */
  autoReconnect?: boolean
  /** 自动重连的最大连续失败次数，超过后回调 onError 并关闭；默认 3 */
  maxAutoReconnects?: number
  /** 收到带 id 的事件时回调最新 last event id（供调用方持久化以跨轮续传） */
  onLastEventId?: (id: string) => void
}

/** `createSSEConnection` 的返回句柄：调用方只需要 `close()`，且可重复调用。 */
export interface SSEHandle {
  close(): void
}

/** 重连间隔：够短让流式看起来"没断"，够长不给正在重启的网关添压。 */
const SSE_RECONNECT_DELAY_MS = 1000

/** 解析一帧（`\n\n` 分隔的一段文本）；字段语义按 SSE 规范，与 EventSource 对齐。 */
function parseSSEFrame(frame: string): { data: string; event: string; id: string } {
  let event = ''
  let id = ''
  const dataLines: string[] = []
  for (const line of frame.split('\n')) {
    if (!line || line.startsWith(':')) continue // 空行与 `: comment` 心跳
    const sep = line.indexOf(':')
    const field = sep === -1 ? line : line.slice(0, sep)
    let value = sep === -1 ? '' : line.slice(sep + 1)
    if (value.startsWith(' ')) value = value.slice(1) // 冒号后的那一个空格属于协议
    if (field === 'data') dataLines.push(value)
    else if (field === 'event') event = value
    else if (field === 'id') id = value
    // retry: 与未知字段：忽略（EventSource 也不把它们交给业务）
  }
  return { data: dataLines.join('\n'), event, id }
}

/**
 * 打开通用事件流（`fetch` + `ReadableStream`）。
 *
 * 为什么抽出来：`/events`（会话）与 `/v1/agent/interactions/stream`（跨会话待办）**都**在网关的
 * `authMW` 之后（internal/api/routes_agent.go:106），同一条"带不出 Authorization 就静默 401"的
 * 规则。两边共用一套身份/帧解析/重连/关闭语义，才不会一处修好、另一处继续烂。
 */
export function openEventStream(
  url: string,
  onMessage: (data: unknown) => void,
  onError?: () => void,
  opts: SSEConnectionOptions = {},
): SSEHandle {
  const { initialLastEventId = '', autoReconnect = false, maxAutoReconnects = 3 } = opts
  const onLastEventId = opts.onLastEventId

  let lastEventId = initialLastEventId
  let closed = false
  let controller: AbortController | null = null
  let reconnectTimer: ReturnType<typeof setTimeout> | null = null
  let failedReconnects = 0

  const requestHeaders = (): Record<string, string> => {
    const headers: Record<string, string> = { Accept: 'text/event-stream' }
    const token = requestToken()
    if (token) headers.Authorization = `Bearer ${token}`
    if (lastEventId) headers['Last-Event-ID'] = lastEventId
    return headers
  }

  const stop = () => {
    closed = true
    if (reconnectTimer !== null) {
      clearTimeout(reconnectTimer)
      reconnectTimer = null
    }
    controller?.abort()
    controller = null
  }

  // 真失败只有这一条路径：先关掉（之后的回调一律静默），再通知调用方 ——
  // 调用方的 onError 负责收掉 loading/计时器，所以它**必须**被调到一次。
  const fail = () => {
    if (closed) return
    stop()
    onError?.()
  }

  const retry = () => {
    if (closed) return
    if (autoReconnect && failedReconnects < maxAutoReconnects) {
      failedReconnects++
      controller = null
      reconnectTimer = setTimeout(() => {
        reconnectTimer = null
        void connect()
      }, SSE_RECONNECT_DELAY_MS)
      return
    }
    fail()
  }

  const handleFrame = (frame: string) => {
    const parsed = parseSSEFrame(frame)
    if (parsed.id) {
      lastEventId = parsed.id
      onLastEventId?.(parsed.id)
    }
    // 空 data 不派发（规范如此）；**命名事件也不进 onMessage** —— 网关的心跳发的是
    // `event: heartbeat`，旧 EventSource 的 onmessage 同样收不到它。这里保持一致，
    // 否则心跳会变成对话里的幽灵消息。
    if (!parsed.data || (parsed.event && parsed.event !== 'message')) return
    try {
      onMessage(JSON.parse(parsed.data))
    } catch {
      // 单帧解析失败不断流（旧实现也是这样吞掉的）
    }
  }

  const connect = async () => {
    if (closed) return
    controller = new AbortController()
    const active = controller
    let response: Response
    try {
      response = await fetch(url, {
        headers: requestHeaders(),
        signal: active.signal,
        // 同源请求靠 httpOnly cookie 认证；身份也可由上面那个 Authorization 头带出
        credentials: 'include',
      })
    } catch {
      if (closed || active.signal.aborted) return // 我们自己关的，不是故障
      retry()
      return
    }
    if (closed) return
    // 非 2xx（401/403/404…）不会自己好：立即失败，别把它拖成「思考中」
    if (!response.ok || !response.body) {
      fail()
      return
    }

    const reader = response.body.getReader()
    const decoder = new TextDecoder()
    let buffer = ''
    try {
      for (;;) {
        const { done, value } = await reader.read()
        if (done) break
        // 连上**并且真的收到过字节**才算成功：只看响应头的话，一个"接受连接就立刻收尾、
        // 什么都不发"的端点会把重连上限每次清零 ⇒ 无限重连，而 loading 一直挂着
        // （2026-11-01 写用例时暴露；旧 EventSource 靠浏览器原生重连，同样没有上限）。
        if (value && value.byteLength > 0) failedReconnects = 0
        // 在**累计缓冲**上归一 CRLF：跨块的 `\r` + `\n` 也能拼回正确的帧边界
        buffer = (buffer + decoder.decode(value, { stream: true })).replace(/\r\n/g, '\n')
        for (;;) {
          const idx = buffer.indexOf('\n\n')
          if (idx === -1) break
          handleFrame(buffer.slice(0, idx))
          buffer = buffer.slice(idx + 2)
          if (closed) return // 回调里可能已经 close()
        }
      }
    } catch {
      if (closed || active.signal.aborted) return
      retry()
      return
    }
    if (closed) return
    retry() // 服务端收尾/流断开：与 EventSource 的 onerror 同语义（要么续上，要么报错）
  }

  void connect()

  return { close: stop }
}

/** 会话事件流：`/events?session_id=…`（身份在 header，见上面那段注释）。 */
export function createSSEConnection(
  sessionId: string,
  onMessage: (data: unknown) => void,
  onError?: () => void,
  opts: SSEConnectionOptions = {},
): SSEHandle {
  return openEventStream(
    `${API_URL}/events?session_id=${encodeURIComponent(sessionId)}`,
    onMessage,
    onError,
    opts,
  )
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

// ── 模板市场 / 共享可见性（2026-08-23 批次2）──
export interface TemplateItem {
  id: string
  type: 'agent' | 'skill'
  name: string
  description: string
  /** 模板内容 —— 具体形状由各工作台自行窄化 */
  payload: Record<string, unknown>
}

export async function listTemplates(type?: 'agent' | 'skill'): Promise<TemplateItem[]> {
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
  /** 所属工作台（五大标识的唯一定义见 src/types/workstation.ts） */
  workstation_type: WorkstationType
  /** 能力类型：tool / service / agent 等 */
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
 * 引擎当前可用的工具（含插件 / MCP 注入的代理工具），带完整 parameters schema
 */
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


// ── 参考价格目录（只读，多方交叉验证）──
export interface ModelPriceSource {
  source: string
  input_per_1k: number
  output_per_1k: number
}

export interface ModelPrice {
  model: string
  provider: string
  input_per_1k: number
  output_per_1k: number
  currency: string
  sources: string[]
  source_count: number
  /** 是否通过多源印证（≥2 个源在容差内一致）；false 表示单源或源间有分歧 */
  verified: boolean
  /** 每个源各自的报价，供「印证来源」一栏展开查看 */
  source_details: ModelPriceSource[]
  verified_at: string
  mixed_per_1k: number
}

export interface ModelPriceCatalog {
  models: ModelPrice[]
  count: number
  unit: string
  note: string
}

/**
 * 拉取参考价格目录。
 *
 * 数据由引擎侧的定价同步任务写入 `model_prices`：只有 **≥2 个公开价格源在 5% 容差内
 * 一致**的价格才会入库（见 `python-engine/app/pricing/sync.py`）。所以表里的每一行
 * 都自带"多源印证"，`sources` 就是参与印证的具体来源。
 *
 * ⚠ 这张表**不参与计费**：账单金额以 `BillingConfig` 为准。这里只回答"公开市场价大概多少"。
 */
export async function listModelPrices(params?: { q?: string; provider?: string; sort?: string }): Promise<ModelPriceCatalog> {
  const resp = await api.get('/v1/pricing/models', { params })
  // 兼容 OK() 包装与裸形态：层级取错会静默变成空列表（listModels 踩过同样的坑）。
  const d = resp.data?.data ?? resp.data
  return {
    models: Array.isArray(d?.models) ? d.models : [],
    count: Number(d?.count ?? 0),
    unit: String(d?.unit ?? ''),
    note: String(d?.note ?? ''),
  }
}
