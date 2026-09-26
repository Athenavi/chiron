import api from './index'

import { t } from '../i18n'
// ── 长期记忆（记忆四层架构 L2 档案卡：跨会话留存） ──

export type MemorySlot = 'identity' | 'preference' | 'decision' | 'fact'
export type MemorySource = 'user_confirmed' | 'derived' | 'tool_written'

export interface MemoryEntry {
  id: string
  slot: string
  slot_label: string
  key: string
  value: string
  confidence: number
  source: string
  source_label: string
  has_embedding: boolean
  access_count: number
  last_accessed_at: string | null
  status: string
  created_at: string
  updated_at: string
}

export interface MemorySearchHit extends MemoryEntry {
  similarity: number
  score: number
}

export interface OrganizeResult {
  backfilled: number
  merged: number
  archived: number
  evicted: number
  errors: string[]
}

export interface OrganizeStatus {
  running: boolean
  started_at: number | null
  finished_at: number | null
  result: OrganizeResult | null
  error: string | null
}

export interface ProfileListResponse {
  entries: MemoryEntry[]
  counts: Record<string, number>
  total: number
  slots: { slot: string; label: string }[]
  organize: OrganizeStatus
}

/** 整卡列表（按槽位分组统计） */
export async function listMemory(includeArchived = false): Promise<ProfileListResponse> {
  const { data } = await api.get('/v1/memory/profile', {
    params: includeArchived ? { archived: 'true' } : {},
  })
  // 兼容两种响应格式：{ success: true, data: {...} } 或 {...}
  return data.data ?? data
}

/** 新建 / 更新一条记忆（同 slot+key 自动更新） */
export async function upsertMemory(body: {
  slot: MemorySlot
  key: string
  value: string
  confidence?: number
  source?: MemorySource
}): Promise<{
  entry: MemoryEntry
  created: boolean
  duplicate_of?: MemoryEntry
  evicted?: number
  /** 与已确认值冲突时后端登记待裁决（不覆盖），由记忆页处理 —— 见 SaveToMemoryDialog */
  conflict?: { old_value?: unknown; new_value?: unknown }
}> {
  const { data } = await api.post('/v1/memory/profile', body)
  return data
}

/** 编辑已有记忆（按 id） */
export async function updateMemory(body: {
  id: string
  key?: string
  value?: string
  confidence?: number
  source?: MemorySource
}): Promise<{ entry: MemoryEntry }> {
  const { data } = await api.put('/v1/memory/profile', body)
  return data
}

/** 删除单条记忆 */
export async function deleteMemory(id: string): Promise<void> {
  await api.delete(`/v1/memory/profile/${encodeURIComponent(id)}`)
}

/** 清空全部记忆（需 confirm=true） */
export async function clearMemory(): Promise<{ deleted: number }> {
  const { data } = await api.post('/v1/memory/profile/clear', { confirm: true })
  return data
}

/** L3 对话摘要：记忆页「摘要」分区与检索结果的 L3 区是同一形态，故共用此类型 */
export interface MemorySummary {
  id: string
  session_id: string
  content: string
  topics: string[]
  turn_range: number[]
  access_count: number
  created_at: number | null
  has_embedding: boolean
  status: string
}

/** @deprecated 用 MemorySummary（同一形态；旧名带检索语义，用在摘要列表里会误导） */
export type MemorySearchSummary = MemorySummary

export interface MemorySearchResponse {
  query: string
  /** results 的匹配方式：语义，或嵌入不可用时的关键词降级 */
  mode: 'semantic' | 'keyword'
  /** L2 命中数（不含 summaries） */
  count: number
  results: MemorySearchHit[]
  /** L3 区：同一 query 在历史对话摘要里的命中 */
  summaries: MemorySearchSummary[]
  summary_count: number
}

/** 检索记忆：返回 L2 条目命中 + L3 摘要命中两个独立结果区 */
export async function searchMemory(
  query: string,
  opts?: { top_k?: number; slot?: MemorySlot },
): Promise<MemorySearchResponse> {
  const { data } = await api.post('/v1/memory/search', {
    query,
    top_k: opts?.top_k ?? 10,
    slot: opts?.slot,
  })
  return data
}

/** 触发异步智能整理（去重 / 归档 / 补嵌入） */
export async function startOrganize(): Promise<{ started: boolean; status: OrganizeStatus }> {
  const { data } = await api.post('/v1/memory/organize')
  return data
}

/** 列出 L3 对话摘要（记忆页「摘要」分区） */
export async function listSummaries(
  limit = 50,
): Promise<{ summaries: MemorySummary[]; count: number }> {
  const { data } = await api.get('/v1/memory/summaries', { params: { limit } })
  return data
}

/** 整理任务状态 */
export async function getOrganizeStatus(): Promise<OrganizeStatus> {
  const { data } = await api.get('/v1/memory/organize/status')
  // 兼容两种响应格式：{ success: true, status: {...} } 或 { status: {...} }
  return data.status ?? data
}

export const MEMORY_SLOTS: { slot: MemorySlot; label: string }[] = [
  { slot: 'identity', label: t('admin.identity') },
  { slot: 'preference', label: t('settings.preferences') },
  { slot: 'decision', label: t('common.key_decisions') },
  { slot: 'fact', label: t('common.long_term_facts') },
]

// ── 冲突裁决 API ──

export interface MemoryConflict {
  conflict_id: string
  slot: string
  item_key: string
  old_value: string
  new_value: string
  source: string
  created_at: number
}

export interface ConflictListResponse {
  conflicts: MemoryConflict[]
  count: number
}

/** 获取待裁决冲突列表 */
export async function listConflicts(): Promise<ConflictListResponse> {
  const { data } = await api.get('/v1/memory/conflicts')
  return data
}

export interface ResolveConflictResponse {
  conflict_id: string
  final_value: string
  resolution: string
  slot: string
  item_key: string
  profile_update?: {
    success: boolean
    item: string | null
  }
}

/**
 * 裁决冲突
 * @param conflictId 冲突 ID
 * @param resolution 裁决方式: 'keep_old' | 'use_new' | 'manual'
 * @param manualValue 手动修改值（仅当 resolution === 'manual'）
 */
export async function resolveConflict(
  conflictId: string,
  resolution: 'keep_old' | 'use_new' | 'manual',
  manualValue?: string,
): Promise<ResolveConflictResponse> {
  const body: { resolution: string; manual_value?: string } = { resolution }
  if (resolution === 'manual' && manualValue !== undefined) {
    body.manual_value = manualValue
  }
  const { data } = await api.post(`/v1/memory/conflicts/${encodeURIComponent(conflictId)}/resolve`, body)
  return data.conflict
}

/** 删除冲突（用户否认） */
export async function deleteConflict(conflictId: string): Promise<{ deleted: string }> {
  const { data } = await api.delete(`/v1/memory/conflicts/${encodeURIComponent(conflictId)}`)
  return data
}
