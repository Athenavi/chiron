import api from './index'

/**
 * 用户级 API Key 自助（P1-3 / T2）的接口封装。
 *
 * 与 `admin.ts` 里的 `listApiKeys` / `addApiKey` **不是一回事**：那一组打的是
 * `/v1/admin/api-keys`，管的是 **LLM 供应商密钥池**（provider / base_url / weight），
 * 属于平台运营配置。这里打 `/v1/api-keys`，是「我自己的账号能用来调网关的凭证」，
 * 普通用户本来就该自助管理。两者曾共用「API Key」这个中文名，极易混淆。
 */

/** 列表里的一行。**不含 `key_hash`** —— 后端类型层面就保证了漏不出去。 */
export interface UserAPIKey {
  id: string
  name: string
  created_at?: string
  /**
   * 创建后**从未被使用**时为空。
   *
   * 注意：截至当前后端版本这一列基本恒为空 —— 鉴权链路会读 `api_keys` 但从不回写。
   * 前端如实显示「未使用过」而不是隐藏该列，补上写入后无需改前端即可生效。
   */
  last_used_at?: string
  expires_at?: string
  /** 已吊销的 key 仍然出现在列表里：用户需要看到「这把是我上周停掉的」 */
  revoked: boolean
}

/** 创建响应：比列表多一个**明文** key。这是整个流程里唯一一次能看到明文。 */
export interface CreatedUserAPIKey extends UserAPIKey {
  key: string
}

export async function listUserApiKeys(): Promise<UserAPIKey[]> {
  const { data } = await api.get('/v1/api-keys')
  // 空列表后端返回 []，这里再兜一层：拿不到 data 时给空数组，
  // 否则模板里 v-for 遇到 undefined 会炸。
  return data?.data ?? []
}

export async function createUserApiKey(name: string): Promise<CreatedUserAPIKey> {
  const { data } = await api.post('/v1/api-keys', { name })
  return data?.data
}

/**
 * 吊销一把 key。
 *
 * 用 DELETE 表达用户视角的「删除」，但后端只是 `revoked = true` —— 保留审计。
 * 重复吊销 / 不存在 / 不是自己的，一律 404（不区分，否则可以枚举别人的 id）。
 */
export async function revokeUserApiKey(id: string): Promise<void> {
  await api.delete(`/v1/api-keys/${encodeURIComponent(id)}`)
}
