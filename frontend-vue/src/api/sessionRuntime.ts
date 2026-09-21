/**
 * 会话运行时状态与遥测 API（网关 internal/api/session_runtime.go）。
 *
 * **单一事实源**：模式 / 模型 / provider / 工具授权 / 上下文激活项，
 * 见 docs/session-runtime-spec.md。存储分层为 Redis 热（TTL 24h）
 * + `unified_sessions.runtime` 持久 —— 刷新、重开会话都不丢。
 *
 * 背景：这些选择此前散落在「会话 llm_config」「组件 ref」「URL query」三处，
 * 且两条提交链路各带一份，缺一份就静默失效（"切了不生效 / 刷新即回退默认"）。
 */
import { api } from './index'

/**
 * 解析后的**生效值 + 来源**：
 * request=本次请求显式 / session=会话 runtime / default=用户或全局默认 /
 * system=系统兜底 / auto=自动路由（未指定 provider）。
 */
export interface ResolvedValue {
  value: string
  source: 'request' | 'session' | 'default' | 'system' | 'auto' | string
}

export interface SessionRuntime {
  mode?: string
  tools_mode?: string
  model?: string
  provider?: string
  compaction?: Record<string, unknown>
  context?: Record<string, unknown>
  updated_at?: string
  updated_by?: string
}

export interface SessionRuntimeView {
  runtime: SessionRuntime
  /** 用户级（users.settings）> 全局级（system_settings.agent）的默认值 */
  defaults?: Record<string, string>
  resolved?: {
    mode?: ResolvedValue
    tools_mode?: ResolvedValue
    model?: ResolvedValue
    provider?: ResolvedValue
  }
}

/** 会话遥测汇总（与 DB 的 turns/billing_records 同口径；实时层缺失时后端回落 DB）。 */
export interface SessionMetrics {
  source: 'redis' | 'db' | string
  totals: {
    turns?: number
    input_tokens?: number
    output_tokens?: number
    cached_tokens?: number
    cache_hits?: number
    cache_hit_rate?: number
    cost_cents?: number
  }
  throughput?: {
    ttft_ms_p50?: number
    output_tps_p50?: number
    output_tps_p95?: number
    sample_turns?: number
  }
  turns?: Array<Record<string, unknown>>
}

export async function getSessionRuntime(sessionId: string): Promise<SessionRuntimeView> {
  const res = await api.get(`/v1/sessions/${encodeURIComponent(sessionId)}/runtime`)
  return res.data?.data ?? res.data
}

/**
 * 局部更新（PATCH 语义）：只传要改的字段。
 * 显式传 `null` 表示**清除**该项，让它回落默认链（请求 > 会话 > 用户默认 > 全局默认）。
 */
export async function putSessionRuntime(
  sessionId: string,
  patch: Partial<
    Record<'mode' | 'tools_mode' | 'model' | 'provider' | 'compaction' | 'context', unknown>
  >,
): Promise<SessionRuntimeView> {
  const res = await api.put(`/v1/sessions/${encodeURIComponent(sessionId)}/runtime`, patch)
  return res.data?.data ?? res.data
}

export async function getSessionMetrics(sessionId: string): Promise<SessionMetrics> {
  const res = await api.get(`/v1/sessions/${encodeURIComponent(sessionId)}/metrics`)
  return res.data?.data ?? res.data
}
