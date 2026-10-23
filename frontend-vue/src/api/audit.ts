import { api } from './index'

// 审计日志行（对齐后端 auditLogRow）
export interface AuditLog {
  id: string
  tenant_id: number
  user_id: string | null
  action: string
  resource_type: string
  resource_id: string | null
  details: unknown
  ip_address: string | null
  created_at: string
}

export interface AuditQueryParams {
  user_id?: string
  action?: string
  resource_type?: string
  from?: string // YYYY-MM-DD 或 RFC3339
  to?: string
  page?: number
  page_size?: number
}

export interface AuditQueryResult {
  data: AuditLog[]
  total: number
  page: number
  per_page: number
}

export async function queryAuditLogs(params: AuditQueryParams): Promise<AuditQueryResult> {
  const { data } = await api.get('/v1/ent/audit', { params })
  return {
    data: data?.data ?? [],
    total: data?.meta?.total ?? 0,
    page: data?.meta?.page ?? 1,
    per_page: data?.meta?.per_page ?? 50,
  }
}
