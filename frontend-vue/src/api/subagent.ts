/**
 * 子 Agent 运行观测 API（网关 internal/api/subagent_handler.go）。
 *
 * 数据源分层：Redis（运行期，TTL 1h）优先 → PostgreSQL（权威）回落；
 * 响应里的 `source` 字段标明本次数据来自哪一层 —— 排查"看不到进度"时先看它。
 *
 * 后端契约见 docs/subagent-design.md §4.2 / §4.4。
 */
import { api } from './index'

export interface SubagentUsage {
  input_tokens?: number
  output_tokens?: number
  steps?: number
}

/** run 视图（Redis 与 DB 两条来源已由后端归一，前端只认这一份契约）。 */
export interface SubagentRunView {
  run_id: string
  parent_run_id?: string
  depth: number
  profile?: string
  status: string
  summary?: string
  usage?: SubagentUsage
  created_at?: string
  redacted_count?: number
  source?: string
}

/** 进度事件（type 前缀 `subagent.`；经 SSE 实时到达，也可从事件端点回放）。 */
export interface SubagentEvent {
  id?: string
  type: string
  run_id: string
  parent_run_id?: string
  depth?: number
  profile?: string
  status?: string
  content?: string
  truncated?: boolean
  usage?: SubagentUsage
}

export interface SubagentRunsResponse {
  runs: SubagentRunView[]
  source: string
}

export interface SubagentEventsResponse {
  events: SubagentEvent[]
  source: string
}

/** 整棵层级树（session_id）或某节点的直接子节点（parent_run_id）。 */
export async function listSubagentRuns(params: {
  sessionId?: string
  parentRunId?: string
}): Promise<SubagentRunsResponse> {
  const { data } = await api.get('/v1/subagent/runs', {
    params: {
      session_id: params.sessionId || undefined,
      parent_run_id: params.parentRunId || undefined,
    },
  })
  return { runs: data?.data?.runs || [], source: data?.data?.source || 'db' }
}

export async function getSubagentRun(runId: string): Promise<SubagentRunView | null> {
  const { data } = await api.get(`/v1/subagent/runs/${encodeURIComponent(runId)}`)
  return data?.data?.run || null
}

/** 输出过程回放：Redis Stream（近实时）→ DB steps（历史）。 */
export async function getSubagentRunEvents(runId: string, limit = 200): Promise<SubagentEventsResponse> {
  const { data } = await api.get(`/v1/subagent/runs/${encodeURIComponent(runId)}/events`, {
    params: { limit },
  })
  return { events: data?.data?.events || [], source: data?.data?.source || 'db' }
}
