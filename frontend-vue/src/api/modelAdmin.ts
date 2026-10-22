/**
 * 模型配置（管理端）：决定 `/chat` 的模型下拉里**能选到什么**。
 *
 * 端点挂载见 internal/api/gateway_router.go 的 `/v1/admin/models*`（此前 adminMux 内
 * 已注册但**未对外暴露**，导致前端无论怎么点都调不到）；实现在 internal/api/admin_ops.go。
 *
 * 为什么必须有它：`/v1/models` 只返回 `llm_models` 里 `enabled` 的行。这些行平时由
 * **模型发现**（按 provider keyset 实时拉取 /models 写入）维护；一旦发现失败
 * （provider 不提供 /models、UA 被拦、网络不可达），可用模型集就是空的
 * —— 此时这里是用户唯一能把模型放进下拉的入口。
 */
import { api } from './index'

export interface AdminModel {
  id: string
  provider: string
  name: string
  display_name?: string
  enabled: boolean
  context_window: number
  created_at?: string
}

function unwrap<T>(payload: unknown): T {
  const p = payload as { data?: unknown } | null | undefined
  return (p?.data !== undefined ? p.data : payload) as T
}

export async function listAdminModels(): Promise<AdminModel[]> {
  const res = await api.get('/v1/admin/models')
  return unwrap<{ models?: AdminModel[] }>(res.data)?.models ?? []
}

/** 新增或按 (provider, name) 覆盖：后端是 ON CONFLICT DO UPDATE，重复添加不会报错 */
export async function upsertAdminModel(payload: {
  provider: string
  name: string
  display_name?: string
  enabled?: boolean
  context_window?: number
}): Promise<void> {
  await api.post('/v1/admin/models', { enabled: true, context_window: 128000, ...payload })
}

export async function updateAdminModel(
  id: string,
  patch: Partial<Pick<AdminModel, 'enabled' | 'context_window' | 'display_name'>>,
): Promise<void> {
  await api.put(`/v1/admin/models/${encodeURIComponent(id)}`, patch)
}

/**
 * 批量启用/禁用（/models 页的「组内全选 → 启用/禁用」）。
 *
 * 为什么不在这里 `Promise.all` 一串单条 PUT：整组开关会一次改几十上百行，
 * 逐个请求除了慢，中途失败还会留下「一半开一半关」的中间态且无从回滚。
 * 后端 `PUT /v1/admin/models/batch` 用一条 `UPDATE ... WHERE id = ANY(...)`
 * 原子完成，并返回**实际影响的行数**（去重后、且不含已不存在的 id）。
 */
export async function batchUpdateAdminModels(ids: string[], enabled: boolean): Promise<number> {
  const res = await api.put('/v1/admin/models/batch', { ids, enabled })
  const data = unwrap<{ updated?: number }>(res.data)
  return Number(data?.updated ?? 0)
}

export async function deleteAdminModel(id: string): Promise<void> {
  await api.delete(`/v1/admin/models/${encodeURIComponent(id)}`)
}

/** provider 候选项（含"是否已配置 key"标记，便于提示用户先配 key） */
export interface ProviderOption {
  id: string
  name: string
  hasKey: boolean
}

interface RawProvider {
  id?: unknown
  name?: unknown
  label?: unknown
  key_count?: unknown
  keyCount?: unknown
}

export async function listProviderOptions(): Promise<ProviderOption[]> {
  const res = await api.get('/v1/admin/llm-providers')
  // 后端字段名在不同版本间漂移（providers/items、key_counts/keyCounts），故按**联合形状**声明，
  // 再在下面用 String()/Number() 收敛成具体的 ProviderOption —— 不引入 any。
  const data = unwrap<{
    providers?: RawProvider[]
    items?: RawProvider[]
    key_counts?: Record<string, number>
    keyCounts?: Record<string, number>
  }>(res.data) ?? {}
  const providers = data.providers ?? data.items ?? []
  const counts: Record<string, number> = data.key_counts ?? data.keyCounts ?? {}
  return providers.map(p => ({
    id: String(p.id ?? p.name ?? ''),
    name: String(p.name ?? p.label ?? p.id ?? ''),
    hasKey: Number(p.key_count ?? p.keyCount ?? counts[String(p.id ?? '')] ?? 0) > 0,
  }))
}
