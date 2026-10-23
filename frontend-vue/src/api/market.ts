import { api } from './index'

// ── 市场条目（MarketItem）──

export interface MarketItem {
  id: string
  type: 'plugin' | 'skill'
  name: string
  version: string
  manifest: unknown
  status: 'draft' | 'published' | 'retired'
  created_by: string | null
  created_at: string
  updated_at: string
}

export async function listMarketItems(params?: { type?: string; status?: string }): Promise<MarketItem[]> {
  const { data } = await api.get('/v1/ent/market/items', { params })
  return data?.data ?? []
}

export async function getMarketItem(id: string): Promise<MarketItem> {
  const { data } = await api.get(`/v1/ent/market/items/${encodeURIComponent(id)}`)
  return data?.data
}

export async function createMarketItem(body: {
  type: 'plugin' | 'skill'
  name: string
  version?: string
  manifest?: unknown
}): Promise<MarketItem> {
  const { data } = await api.post('/v1/ent/market/items', body)
  return data?.data
}

export async function updateMarketItem(id: string, body: {
  version?: string
  manifest?: unknown
}): Promise<void> {
  await api.put(`/v1/ent/market/items/${encodeURIComponent(id)}`, body)
}

export async function deleteMarketItem(id: string): Promise<void> {
  await api.delete(`/v1/ent/market/items/${encodeURIComponent(id)}`)
}

export async function publishMarketItem(id: string): Promise<void> {
  await api.post(`/v1/ent/market/items/${encodeURIComponent(id)}/publish`)
}

export async function retireMarketItem(id: string): Promise<void> {
  await api.post(`/v1/ent/market/items/${encodeURIComponent(id)}/retire`)
}

// ── 租户授权（MarketGrant）──

export interface MarketGrant {
  item_id: string
  tenant_id: number
  enabled: boolean
  installed_at: string
}

export async function listMarketGrants(params?: { item_id?: string; tenant_id?: number }): Promise<MarketGrant[]> {
  const { data } = await api.get('/v1/ent/market/grants', { params })
  return data?.data ?? []
}

export async function grantMarketItem(body: { item_id: string; tenant_id: number; enabled?: boolean }): Promise<void> {
  await api.post('/v1/ent/market/grants', body)
}

export async function updateMarketGrant(itemID: string, tenantID: string, enabled: boolean): Promise<void> {
  await api.put(`/v1/ent/market/grants/${encodeURIComponent(itemID)}/${encodeURIComponent(tenantID)}`, { enabled })
}

export async function deleteMarketGrant(itemID: string, tenantID: string): Promise<void> {
  await api.delete(`/v1/ent/market/grants/${encodeURIComponent(itemID)}/${encodeURIComponent(tenantID)}`)
}
