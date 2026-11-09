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

// ── 审计导出（M3）──
//
// 用 `responseType: 'blob'`：响应体是 CSV 文件而不是 JSON。
// 代价是错误响应体也变成 Blob，调用方必须用 `blobErrorPayload` 读回文本，
// 否则拿不到"记录太多，请缩小范围"这类唯一有用的提示。
//
// 过滤条件必须与列表一致：用户点了"导出"，期望拿到的是**当前筛选条件下的全量**。

export async function exportAuditCsv(params: AuditQueryParams): Promise<Blob> {
  const resp = await api.get('/v1/ent/audit.csv', {
    params: {
      user_id: params.user_id,
      action: params.action,
      resource_type: params.resource_type,
      from: params.from,
      to: params.to,
    },
    responseType: 'blob',
  })
  return resp.data as Blob
}

/**
 * 审计导出的文件名。
 *
 * 纯 ASCII、不含冒号（Windows 非法字符）。不依赖后端 `Content-Disposition`：
 * 跨域场景下浏览器默认读不到该响应头，前端自己生成更可靠 ——
 * 与 `utils/transactionExport.ts` 的取舍相同。
 */
export function auditExportFileName(now: Date = new Date()): string {
  const pad = (n: number) => String(n).padStart(2, '0')
  const stamp = `${now.getUTCFullYear()}${pad(now.getUTCMonth() + 1)}${pad(now.getUTCDate())}T${pad(now.getUTCHours())}${pad(now.getUTCMinutes())}`
  return `audit_${stamp}.csv`
}
