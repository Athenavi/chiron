/**
 * Super-agent 接管 API（网关 internal/api/superagent.go、superagent_schedule.go）。
 *
 * 判据是用户自己：全部端点只按 `user_id` 收窄，不需要 admin。
 * 运行态以服务端快照为准，SSE 只做增量 —— 刷新页面后遮罩能否对上，全看这里。
 */
import { api } from './index'

export interface TakeoverWindow {
  kind: 'countdown' | 'schedule'
  duration_seconds: number
  /** 两拍之间的间隔；0 表示用服务端默认值（180s）。 */
  tick_seconds: number
  /** 单窗口回合数上限；0 表示不设限（只受 token 预算约束）。 */
  max_rounds: number
}

export interface TakeoverBudget {
  /** 硬上限；0 表示不限量。提示是软的，到了这里就停。 */
  token_cap: number
}

export interface TakeoverConfig {
  enabled: boolean
  session_ids: string[]
  window: TakeoverWindow
  budget: TakeoverBudget
}

/** 一条正在生效的接管（`status` 只会是 active 或 stopping）。 */
export interface Takeover {
  id: string
  session_id: string
  status: string
  started_at: string
  ends_at: string
  stop_reason?: string
  /** 上一拍的结果，如 `skipped_busy`。它说的是"这一拍没跑成"，接管仍在进行。 */
  last_status?: string
  token_spent: number
  token_cap: number
}

export interface SuperAgentSnapshot {
  config: TakeoverConfig
  takeovers: Takeover[]
}

export interface SuperAgentSchedule {
  id: string
  name?: string
  schedule: string
  window_seconds: number
  session_ids: string[]
  enabled: boolean
  last_run_at?: string
  last_status?: string
}

export interface ScheduleInput {
  name: string
  schedule: string
  window_seconds: number
  session_ids: string[]
  enabled: boolean
}

/** 会话下拉项（`GET /v1/conversations` 的子集）。 */
export interface SelectableSession {
  id: string
  title?: string
  updated_at?: string
}

function unwrap<T>(payload: unknown, fallback: T): T {
  if (payload && typeof payload === 'object') return payload as T
  return fallback
}

export async function getSuperAgentSnapshot(sessionId?: string): Promise<SuperAgentSnapshot> {
  const { data } = await api.get('/v1/superagent/takeovers', {
    params: sessionId ? { session_id: sessionId } : undefined,
  })
  return unwrap(data?.data, {
    config: { enabled: false, session_ids: [], window: { kind: 'countdown', duration_seconds: 3600, tick_seconds: 180, max_rounds: 0 }, budget: { token_cap: 0 } },
    takeovers: [],
  })
}

export interface SaveConfigInput {
  enabled: boolean
  session_ids: string[]
  duration_seconds: number
  token_cap: number
  tick_seconds: number
  max_rounds: number
}

/** 开关与开窗口是同一次写入：分开会留下"开关打开、却没有接管在跑"的假象。 */
export async function saveSuperAgentConfig(input: SaveConfigInput): Promise<SuperAgentSnapshot> {
  const { data } = await api.put('/v1/superagent/config', input)
  return unwrap(data?.data, { config: {} as TakeoverConfig, takeovers: [] })
}

/** 打断接管。返回的是"已受理"（stopping），终态由快照收敛。 */
export async function stopTakeover(id: string): Promise<{ status: string; session_id: string }> {
  const { data } = await api.post(`/v1/superagent/takeovers/${encodeURIComponent(id)}/stop`)
  return { status: data?.data?.status || 'stopping', session_id: data?.data?.session_id || '' }
}

export async function listSchedules(): Promise<{ schedules: SuperAgentSchedule[]; limit: number }> {
  const { data } = await api.get('/v1/superagent/schedules')
  return { schedules: data?.data?.schedules || [], limit: data?.data?.limit ?? 10 }
}

export async function createSchedule(input: ScheduleInput): Promise<SuperAgentSchedule> {
  const { data } = await api.post('/v1/superagent/schedules', input)
  return data?.data
}

export async function updateSchedule(id: string, input: ScheduleInput): Promise<SuperAgentSchedule> {
  const { data } = await api.put(`/v1/superagent/schedules/${encodeURIComponent(id)}`, input)
  return data?.data
}

export async function deleteSchedule(id: string): Promise<void> {
  await api.delete(`/v1/superagent/schedules/${encodeURIComponent(id)}`)
}

/** 立即试跑：不等 cron 到点，用于确认表达式与窗口配置有效。 */
export async function triggerSchedule(id: string): Promise<{ status: string; opened: number }> {
  const { data } = await api.post(`/v1/superagent/schedules/${encodeURIComponent(id)}/trigger`)
  return { status: data?.data?.status || 'accepted', opened: data?.data?.opened ?? 0 }
}

/** 可选会话列表：接管只能作用于自己的会话，所以直接取个人会话列表。 */
export async function listSelectableSessions(): Promise<SelectableSession[]> {
  const { data } = await api.get('/v1/conversations', { params: { per_page: 200 } })
  const rows = data?.data?.items ?? data?.data ?? []
  if (!Array.isArray(rows)) return []
  return rows
    .filter((r: { id?: unknown }) => typeof r?.id === 'string' && r.id)
    .map((r: { id: string; title?: string; updated_at?: string }) => ({
      id: r.id,
      title: r.title,
      updated_at: r.updated_at,
    }))
}
