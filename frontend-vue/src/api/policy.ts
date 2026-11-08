import { api } from './index'

// ── 隐私策略（EntTenantPolicy）──

export interface TenantPrivacy {
  tenant_id: number
  privacy_mode: boolean
  data_retention_days: number
  training_allowed: boolean
  redaction_rules: unknown
  updated_at: string
}

export async function getPrivacy(): Promise<TenantPrivacy> {
  const { data } = await api.get('/v1/ent/privacy')
  return data?.data
}

export async function putPrivacy(body: {
  privacy_mode: boolean
  data_retention_days: number
  training_allowed: boolean
  redaction_rules: unknown
}): Promise<TenantPrivacy> {
  const { data } = await api.put('/v1/ent/privacy', body)
  return data?.data
}
