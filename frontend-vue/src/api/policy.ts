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

// ── 模型策略（EntModelPolicy）──

export interface ModelPolicy {
  id: string
  tenant_id: number
  role_id: string | null
  allowed_models: string[]
  per_model_limits: Record<string, Record<string, number>>
  created_at: string
  updated_at: string
}

export async function listModelPolicies(): Promise<ModelPolicy[]> {
  const { data } = await api.get('/v1/ent/model-policies')
  return data?.data ?? []
}

export async function createModelPolicy(body: {
  role_id?: string
  allowed_models: string[]
  per_model_limits?: Record<string, Record<string, number>>
}): Promise<ModelPolicy> {
  const { data } = await api.post('/v1/ent/model-policies', body)
  return data?.data
}

export async function updateModelPolicy(id: string, body: {
  role_id?: string
  allowed_models?: string[]
  per_model_limits?: Record<string, Record<string, number>>
}): Promise<void> {
  await api.put(`/v1/ent/model-policies/${encodeURIComponent(id)}`, body)
}

export async function deleteModelPolicy(id: string): Promise<void> {
  await api.delete(`/v1/ent/model-policies/${encodeURIComponent(id)}`)
}
