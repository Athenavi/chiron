import { api } from './index'

// ── 类型（对齐后端 EntQuotaPool / EntQuotaAllocation / usageRow）──

export interface QuotaPool {
  id: string
  tenant_id: number
  resource_type: 'token' | 'storage_mb' | 'concurrency' | 'credits'
  total_amount: number // 0 = 无限制
  period: 'daily' | 'monthly'
  created_at: string
  updated_at: string
}

export interface QuotaPoolWithAllocated extends QuotaPool {
  allocated: number
}

export interface QuotaAllocation {
  id: string
  pool_id: string
  target_type: 'group' | 'user'
  target_id: string
  amount: number
  created_at: string
}

export interface QuotaUsageRow {
  pool_id: string
  resource_type: string
  period: string
  total_amount: number
  used: number
  usage_ratio: number
  source: string
}

export interface QuotaUsage {
  tenant_id: number
  as_of: string
  pools: QuotaUsageRow[]
}

// ── 配额池 ──

export async function listQuotas(tenantID?: string): Promise<{ pools: QuotaPoolWithAllocated[]; total: number }> {
  const { data } = await api.get('/v1/ent/quotas', { params: { tenant_id: tenantID } })
  return { pools: data?.pools ?? [], total: data?.total ?? 0 }
}

export async function getQuota(id: string): Promise<{ pool: QuotaPool; allocations: QuotaAllocation[]; allocated: number }> {
  const { data } = await api.get(`/v1/ent/quotas/${encodeURIComponent(id)}`)
  return { pool: data?.pool, allocations: data?.allocations ?? [], allocated: data?.allocated ?? 0 }
}

export async function createQuota(body: {
  tenant_id: number
  resource_type: string
  total_amount: number
  period?: string
}): Promise<QuotaPool> {
  const { data } = await api.post('/v1/ent/quotas', body)
  return data?.data
}

export async function updateQuota(id: string, body: {
  resource_type?: string
  total_amount?: number
  period?: string
}): Promise<void> {
  await api.put(`/v1/ent/quotas/${encodeURIComponent(id)}`, body)
}

export async function deleteQuota(id: string): Promise<void> {
  await api.delete(`/v1/ent/quotas/${encodeURIComponent(id)}`)
}

// ── 分配 ──

export async function createAllocation(poolID: string, body: {
  target_type: 'group' | 'user'
  target_id: string
  amount: number
}): Promise<void> {
  await api.post(`/v1/ent/quotas/${encodeURIComponent(poolID)}/allocations`, body)
}

export async function deleteAllocation(poolID: string, allocID: string): Promise<void> {
  await api.delete(`/v1/ent/quotas/${encodeURIComponent(poolID)}/allocations/${encodeURIComponent(allocID)}`)
}

// ── 用量 ──

export async function getQuotaUsage(tenantID: string): Promise<QuotaUsage> {
  const { data } = await api.get('/v1/ent/quotas/usage', { params: { tenant_id: tenantID } })
  return data
}
