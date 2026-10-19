/**
 * 知识库 API 客户端（PageIndex 版）。
 *
 * 与旧实现的三点差异都体现在类型上：
 * 1. `provider`（selfhosted | cloud）取代了 `type`（wiki | rag）；
 * 2. 文档带 `pageindex_doc_id` / `page_count`，不再有 `chunk_count`；
 * 3. 查询返回**答案 + 引用**，不是 top-k 片段。
 *
 * 所有知识库调用集中在这里：视图里不再出现裸 `api.get('/v1/kb/...')`，
 * 契约变化时只有一个地方要改。
 */
import { api } from './index'

export type ProviderName = 'selfhosted' | 'cloud'
export type KbStatus = 'disabled' | 'indexing' | 'ready' | 'error'
export type KbVisibility = 'public' | 'private' | 'tenant'

export interface KnowledgeBase {
  id: string
  name: string
  description: string
  provider: ProviderName
  visibility: KbVisibility
  status: KbStatus
  document_count: number
  total_size_bytes: number
  credits_consumed: number
  error_message: string
  created_at: string
  updated_at: string
}

export interface KnowledgeDocument {
  id: string
  knowledge_base_id: string
  name: string
  file_type: string
  file_size_bytes: number
  page_count: number
  status: 'pending' | 'indexing' | 'ready' | 'error'
  pageindex_doc_id: string
  error_message: string
  created_at: string
  updated_at: string
}

export interface Citation {
  doc_name: string
  page: number | null
  block_id: string
  snippet: string
}

export interface KnowledgeAnswer {
  provider: string
  answer: string
  citations: Citation[]
}

export interface ProviderOption {
  name: ProviderName
  label: string
  available: boolean
  status: string
  detail: string
}

export interface KnowledgeSettings {
  provider: ProviderName | ''
  status: string
  error_message: string
  cloud_key_configured: boolean
  providers: ProviderOption[]
}

export interface VerifyResult {
  ok: boolean
  status: string
  detail: string
}

/** 网关会把引擎响应包一层 `{data: ...}`；两种形状都兼容。 */
function unwrap<T>(resp: { data?: unknown }): T {
  const outer = resp?.data as { data?: unknown } | undefined
  return (outer && typeof outer === 'object' && 'data' in outer ? outer.data : outer) as T
}

// ── 知识库 ──

export async function listKnowledgeBases(): Promise<KnowledgeBase[]> {
  const resp = await api.get('/v1/kb')
  const body = unwrap<{ knowledge_bases?: KnowledgeBase[] }>(resp)
  return body?.knowledge_bases ?? []
}

export async function createKnowledgeBase(body: {
  name: string
  description?: string
  provider?: ProviderName
  visibility?: KbVisibility
}): Promise<KnowledgeBase> {
  const resp = await api.post('/v1/kb', body)
  return unwrap<KnowledgeBase>(resp)
}

export async function getKnowledgeBase(id: string): Promise<KnowledgeBase> {
  const resp = await api.get(`/v1/kb/${id}`)
  return unwrap<KnowledgeBase>(resp)
}

export async function updateKnowledgeBase(
  id: string,
  body: { name?: string; description?: string; visibility?: KbVisibility },
): Promise<unknown> {
  const resp = await api.put(`/v1/kb/${id}`, body)
  return unwrap<unknown>(resp)
}

export async function deleteKnowledgeBase(id: string): Promise<unknown> {
  const resp = await api.delete(`/v1/kb/${id}`)
  return unwrap<unknown>(resp)
}

export async function setKnowledgeBaseVisibility(
  id: string,
  visibility: KbVisibility,
): Promise<unknown> {
  const resp = await api.put(`/v1/kb/${id}/visibility`, { visibility })
  return unwrap<unknown>(resp)
}

// ── 文档 ──

export async function listDocuments(kbId: string): Promise<KnowledgeDocument[]> {
  const resp = await api.get(`/v1/kb/${kbId}/documents`)
  const body = unwrap<{ documents?: KnowledgeDocument[] }>(resp)
  return body?.documents ?? []
}

export async function uploadDocument(
  kbId: string,
  body: { name: string; file_type?: string; file_size_bytes?: number; content: string },
): Promise<KnowledgeDocument> {
  const resp = await api.post(`/v1/kb/${kbId}/documents`, body)
  return unwrap<KnowledgeDocument>(resp)
}

export async function deleteDocument(kbId: string, docId: string): Promise<unknown> {
  const resp = await api.delete(`/v1/kb/${kbId}/documents`, { params: { doc_id: docId } })
  return unwrap<unknown>(resp)
}

export async function previewDocument(
  kbId: string,
  docId: string,
): Promise<{ data?: KnowledgeDocument & { content?: string } }> {
  const resp = await api.get(`/v1/kb/${kbId}/documents/${docId}/preview`)
  return unwrap<{ data?: KnowledgeDocument & { content?: string } }>(resp) ?? {}
}

// ── 索引与问答 ──

export async function buildKnowledgeBase(
  kbId: string,
): Promise<{ status: string; task_id: string; provider: string }> {
  const resp = await api.post(`/v1/kb/${kbId}/build`, {})
  return unwrap<{ status: string; task_id: string; provider: string }>(resp)
}

export async function queryKnowledgeBase(kbId: string, query: string): Promise<KnowledgeAnswer> {
  const resp = await api.post(`/v1/kb/${kbId}/query`, { query })
  return unwrap<KnowledgeAnswer>(resp)
}

// ── 用户级设置 ──

export async function getKnowledgeSettings(): Promise<KnowledgeSettings> {
  const resp = await api.get('/v1/kb/settings')
  return unwrap<KnowledgeSettings>(resp)
}

export async function putKnowledgeSettings(body: {
  provider: ProviderName
  cloud_api_key?: string
}): Promise<{ provider: string; status: string; cloud_key_configured: boolean }> {
  const resp = await api.put('/v1/kb/settings', body)
  return unwrap<{ provider: string; status: string; cloud_key_configured: boolean }>(resp)
}

export async function verifyKnowledgeSettings(): Promise<VerifyResult> {
  const resp = await api.post('/v1/kb/settings/verify', {})
  return unwrap<VerifyResult>(resp)
}
