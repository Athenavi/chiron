import api from './index'

// ── 人机验证（公开配置 + 管理端） ──

export interface CaptchaPublicConfig {
  enabled: boolean
  provider?: string
  site_key?: string
  verify_url?: string
}

export interface CaptchaAdminConfig {
  provider: string
  site_key: string
  secret: string
  verify_url?: string
  enabled: boolean
}

/** 登录页拉取验证码组件参数（未启用时 enabled=false） */
export async function getCaptchaPublicConfig(): Promise<CaptchaPublicConfig> {
  const { data } = await api.get('/v1/auth/captcha/config')
  return data?.data ?? { enabled: false }
}

export async function getCaptchaAdminConfig(): Promise<CaptchaAdminConfig> {
  const { data } = await api.get('/v1/ent/captcha/config')
  return data?.data
}

export async function updateCaptchaConfig(body: Partial<{
  provider: string
  site_key: string
  secret: string
  verify_url: string
  enabled: boolean
}>): Promise<CaptchaAdminConfig> {
  const { data } = await api.put('/v1/ent/captcha/config', body)
  return data?.data
}

// ── 三方登录（公开发现 + 用户自助绑定 + 管理端 CRUD） ──

export interface SsoPublicProvider {
  id: string
  name: string
  display_name: string
  provider_type: string
  icon: string
  sort_order: number
  protocol: string
}

export async function listPublicSsoProviders(): Promise<SsoPublicProvider[]> {
  const { data } = await api.get('/v1/auth/sso/providers')
  return data?.data ?? []
}

/**
 * 登录接口返回的用户信息。
 *
 * 与 `stores/auth.ts` 的 `User` 是**子集**关系：后者多一个可选的 `settings`
 * （由 `GET /v1/auth/profile` 下发、`PUT` 局部合并）。因此这里的结构可以直接
 * 交给 `authStore.applySession(token, user)`。
 */
export interface AuthUser {
  id: string
  email: string
  name: string
  role: string
  tenant_id: number
}

export interface UserIdentity {
  id: string
  provider_name: string
  provider_type: string
  subject: string
  email: string
  created_at: string
}

export async function listIdentities(): Promise<UserIdentity[]> {
  const { data } = await api.get('/v1/auth/sso/identities')
  return data?.data ?? []
}

export async function deleteIdentity(id: string): Promise<void> {
  await api.delete(`/v1/auth/sso/identities/${encodeURIComponent(id)}`)
}

export async function setPassword(body: { current_password?: string; new_password: string }): Promise<void> {
  await api.post('/v1/auth/password', body)
}

export interface SsoProvider {
  id: string
  name: string
  issuer: string
  client_id: string
  client_secret: string
  scopes: string[]
  enabled: boolean
  auto_provision: boolean
  role_mapping: Record<string, string>
  protocol: string
  provider_type: string
  display_name: string
  icon: string
  sort_order: number
  auth_url: string
  token_url: string
  userinfo_url: string
  extra: Record<string, string>
  created_at?: string
  updated_at?: string
}

export async function listSsoProviders(): Promise<SsoProvider[]> {
  const { data } = await api.get('/v1/ent/sso/providers')
  return data?.data ?? []
}

export async function createSsoProvider(body: Partial<SsoProvider>): Promise<SsoProvider> {
  const { data } = await api.post('/v1/ent/sso/providers', body)
  return data?.data
}

export async function updateSsoProvider(id: string, body: Partial<SsoProvider>): Promise<SsoProvider> {
  const { data } = await api.put(`/v1/ent/sso/providers/${encodeURIComponent(id)}`, body)
  return data?.data
}

export async function deleteSsoProvider(id: string): Promise<void> {
  await api.delete(`/v1/ent/sso/providers/${encodeURIComponent(id)}`)
}

/** 构造三方登录跳转地址（mode=bind 用于个人中心绑定） */
export function ssoLoginURL(providerId: string, mode?: 'bind'): string {
  const base = (api.defaults?.baseURL || '').replace(/\/$/, '')
  const u = `${base}/v1/auth/sso/login/${encodeURIComponent(providerId)}`
  return mode === 'bind' ? `${u}?mode=bind` : u
}

// ── 短信验证码登录（公开流程 + 用户自助绑定 + 管理端配置） ──

export interface SmsStatus {
  enabled: boolean
  login_enabled: boolean
}

/** 登录页据此决定是否展示"短信登录"标签页 */
export async function getSmsStatus(): Promise<SmsStatus> {
  const { data } = await api.get('/v1/auth/sms/status')
  return data?.data ?? { enabled: false, login_enabled: false }
}

export interface SendSmsCodeResult {
  status: string
  expire_seconds: number
  interval: number
}

/** 发送验证码（防滥用：人机验证 + 发送冷却 + 每日上限） */
export async function sendSmsCode(body: {
  phone: string
  purpose?: 'login' | 'bind'
  captcha_token?: string
  captcha_randstr?: string
}): Promise<SendSmsCodeResult> {
  const { data } = await api.post('/v1/auth/sms/code', body)
  return data?.data
}

export async function smsLogin(body: {
  phone: string
  code: string
  captcha_token?: string
  captcha_randstr?: string
}): Promise<{ token: string; user: AuthUser }> {
  const { data } = await api.post('/v1/auth/sms/login', body)
  return data?.data
}

/** 当前绑定手机号 */
export async function getSmsBind(): Promise<{ phone: string; bound: boolean }> {
  const { data } = await api.get('/v1/auth/sms/bind')
  return data?.data
}

export async function bindPhone(body: { phone: string; code: string }): Promise<void> {
  await api.post('/v1/auth/sms/bind', body)
}

export async function unbindPhone(): Promise<void> {
  await api.delete('/v1/auth/sms/bind')
}

export interface SmsAdminConfig {
  provider: string
  sign_name: string
  template_id: string
  access_key_id: string
  secret: string
  endpoint: string
  code_ttl_seconds: number
  send_interval_seconds: number
  daily_limit: number
  login_enabled: boolean
  auto_register: boolean
  enabled: boolean
  exists?: boolean
}

export async function getSmsAdminConfig(): Promise<SmsAdminConfig> {
  const { data } = await api.get('/v1/ent/sms/config')
  const cfg: SmsAdminConfig = data?.data
  // 前端脱敏处理（纵深防御）：仅展示 secret 首尾各 2 字符，中间用 *** 替代
  // 注意：后端 API 已主动脱敏 secret 字段（maskedSecret），前端脱敏仅作为纵深防御
  if (cfg && cfg.secret && cfg.secret.length > 6) {
    cfg.secret = cfg.secret.slice(0, 2) + '***' + cfg.secret.slice(-2)
  }
  return cfg
}

export async function updateSmsConfig(body: Partial<{
  provider: string
  sign_name: string
  template_id: string
  access_key_id: string
  secret: string
  endpoint: string
  code_ttl_seconds: number
  send_interval_seconds: number
  daily_limit: number
  login_enabled: boolean
  auto_register: boolean
  enabled: boolean
}>): Promise<SmsAdminConfig> {
  const { data } = await api.put('/v1/ent/sms/config', body)
  const cfg: SmsAdminConfig = data?.data
  // 前端脱敏处理（纵深防御）：仅展示 secret 首尾各 2 字符，中间用 *** 替代
  // 注意：后端 API 已主动脱敏 secret 字段（maskedSecret），前端脱敏仅作为纵深防御
  if (cfg && cfg.secret && cfg.secret.length > 6) {
    cfg.secret = cfg.secret.slice(0, 2) + '***' + cfg.secret.slice(-2)
  }
  return cfg
}

/** 手机号校验：可选 + 前缀 + 5-20 位数字（与后端 ValidateSmsPhone 一致） */
export function isValidPhone(phone: string): boolean {
  const p = (phone || '').trim()
  if (!p || p.length > 21) return false
  const digits = p.startsWith('+') ? p.slice(1) : p
  return /^\d{5,20}$/.test(digits)
}

// ── 邮件：邮箱验证码登录 / 注册邮箱验证 / 密码重置 / 管理端配置 ──

export interface EmailStatus {
  enabled: boolean
  login_enabled: boolean
  register_verify: boolean
  reset_enabled: boolean
}

/** 登录/注册页据此决定是否展示邮箱登录、注册验证码、找回密码入口 */
export async function getEmailStatus(): Promise<EmailStatus> {
  const { data } = await api.get('/v1/auth/email/status')
  return data?.data ?? { enabled: false, login_enabled: false, register_verify: false, reset_enabled: false }
}

export interface SendEmailCodeResult {
  status: string
  expire_seconds: number
  interval: number
}

/** 发送邮箱验证码（防滥用：人机验证 + 发送冷却 + 每日上限） */
export async function sendEmailCode(body: {
  email: string
  purpose?: 'login' | 'register'
  captcha_token?: string
  captcha_randstr?: string
}): Promise<SendEmailCodeResult> {
  const { data } = await api.post('/v1/auth/email/code', body)
  return data?.data
}

export async function emailLogin(body: {
  email: string
  code: string
  captcha_token?: string
  captcha_randstr?: string
}): Promise<{ token: string; user: AuthUser }> {
  const { data } = await api.post('/v1/auth/email/login', body)
  return data?.data
}

/** 申请密码重置：后端恒返回 sent（不暴露邮箱是否存在） */
export async function requestPasswordReset(body: {
  email: string
  captcha_token?: string
  captcha_randstr?: string
}): Promise<{ status: string; expire_seconds: number }> {
  const { data } = await api.post('/v1/auth/password/reset/request', body)
  return data?.data
}

/** 用邮件里的令牌重设密码 */
export async function confirmPasswordReset(body: {
  token: string
  password: string
  captcha_token?: string
  captcha_randstr?: string
}): Promise<void> {
  await api.post('/v1/auth/password/reset/confirm', body)
}

export interface MailAdminConfig {
  provider: 'smtp' | 'qingchen'
  enabled: boolean
  /** SMTP 通道 */
  smtp_host: string
  smtp_port: number
  smtp_username: string
  smtp_password: string
  smtp_security: 'none' | 'starttls' | 'ssl'
  smtp_skip_verify: boolean
  /** 晴辰云邮（HTTP API）通道 */
  api_base_url: string
  api_key: string
  api_channel_id: number
  api_template_id: number
  /** 发件身份 */
  from_address: string
  from_name: string
  reply_to: string
  /** 站点信息 */
  site_name: string
  app_base_url: string
  /** 能力开关 */
  login_enabled: boolean
  register_verify: boolean
  auto_register: boolean
  reset_enabled: boolean
  welcome_enabled: boolean
  /** 邮件模板（留空用内置默认） */
  code_subject: string
  code_body: string
  welcome_subject: string
  welcome_body: string
  reset_subject: string
  reset_body: string
  /** 限流与超时 */
  code_ttl_seconds: number
  send_interval_seconds: number
  daily_limit: number
  timeout_seconds: number
  exists?: boolean
  /** true = 本租户尚未单独配置，当前回退平台默认 SMTP */
  using_default?: boolean
}

export async function getMailAdminConfig(): Promise<MailAdminConfig> {
  const { data } = await api.get('/v1/ent/mail/config')
  return data?.data
}

export async function updateMailConfig(body: Partial<MailAdminConfig>): Promise<MailAdminConfig> {
  const { data } = await api.put('/v1/ent/mail/config', body)
  return data?.data
}

/** 用当前配置真实发一封测试邮件（启用前先确认能不能通） */
export async function sendMailTest(to: string): Promise<{ status: string; to: string }> {
  const { data } = await api.post('/v1/ent/mail/test', { to })
  return data?.data
}

/** 邮箱校验（与后端 ValidateMailAddress 一致：单段域名与换行注入都拒绝） */
export function isValidEmail(email: string): boolean {
  const e = (email || '').trim()
  if (!e || e.length > 254 || /[\r\n]/.test(e)) return false
  const at = e.lastIndexOf('@')
  if (at <= 0 || at === e.length - 1) return false
  const domain = e.slice(at + 1)
  return domain.includes('.') && !domain.startsWith('.') && !domain.endsWith('.') && !/\s/.test(e)
}

// ── 双因子认证（MFA / TOTP）──
//
// 后端是**两阶段启用**：setup 生成密钥但不启用，必须再用 /enable 提交一次
// 正确验证码才算启用。原因是扫码后关掉页面的用户，若被算作"已启用"，
// 下次登录会被要求输入一个他从未成功配置过的验证码 —— 直接锁死账号。

export interface MfaStatus {
  enabled: boolean
  /** 已生成密钥但尚未确认：UI 必须提示"还没配完"，否则用户以为开了 */
  pending_confirmation: boolean
  recovery_codes_left: number
}

export interface MfaSetupResult {
  secret: string
  otpauth_url: string
  recovery_codes: string[]
  /** true 表示此前已启用，本次是重置 —— 旧验证器将失效 */
  already_enabled: boolean
}

export async function getMfaStatus(): Promise<MfaStatus> {
  const { data } = await api.get('/v1/auth/mfa/status')
  return data?.data ?? { enabled: false, pending_confirmation: false, recovery_codes_left: 0 }
}

export async function setupMfa(): Promise<MfaSetupResult> {
  const { data } = await api.post('/v1/auth/mfa/setup')
  return data?.data
}

export async function enableMfa(code: string): Promise<void> {
  await api.post('/v1/auth/mfa/enable', { code })
}

/** 关闭 MFA：验证码与密码**任一**即可（手机丢了时用密码）。 */
export async function disableMfa(body: { code?: string; password?: string }): Promise<void> {
  await api.post('/v1/auth/mfa/disable', body)
}

/** 登录第二步：用挑战令牌 + 验证码换正式 token。 */
export async function verifyMfa(challengeToken: string, code: string): Promise<{ token: string; user: AuthUser }> {
  const { data } = await api.post('/v1/auth/mfa/verify', {
    challenge_token: challengeToken,
    code,
  })
  return data?.data
}

// ── 账号注销 ──
//
// 后端是"冻结 → 7 天宽限 → 到期归档并清除"三步。申请后账号除**导出**与
// **撤销**外的所有操作都会被服务端 403（不是前端隐藏按钮：绕过前端照样被拦）。
//
// 因此前端必须如实展示剩余天数 —— 用户要知道自己还有多久能改主意。

export interface AccountDeletionStatus {
  status: 'active' | 'pending_deletion' | 'purged'
  requested_at?: string
  due_at?: string
  /** 距自动清除的剩余天数（后端按服务端时钟计算，客户端时间不可信） */
  days_remaining: number
  can_export: boolean
  can_cancel: boolean
}

export async function getAccountDeletionStatus(): Promise<AccountDeletionStatus> {
  const { data } = await api.get('/v1/me/deletion')
  return data?.data ?? { status: 'active', days_remaining: 0, can_export: false, can_cancel: false }
}

/** 申请注销并冻结资产。`confirm` 必填：冻结是破坏性动作，不能由误触触发。 */
export async function requestAccountDeletion(body: {
  password?: string
  confirm: boolean
}): Promise<AccountDeletionStatus> {
  const { data } = await api.post('/v1/me/deletion', body)
  return data?.data
}

/** 在宽限期内撤销注销，资产按冻结前的状态精确恢复。 */
export async function cancelAccountDeletion(): Promise<AccountDeletionStatus> {
  const { data } = await api.post('/v1/me/deletion/cancel')
  return data?.data
}

/** 下载个人信息（数据可携带权）。注销冻结期仍可用。 */
export async function downloadMyData(): Promise<void> {
  const { data } = await api.get('/v1/me/export', { responseType: 'blob' })
  const url = URL.createObjectURL(data as Blob)
  const a = document.createElement('a')
  a.href = url
  a.download = 'chiron-personal-data.json'
  a.click()
  // 必须释放：否则 blob 会一直占着内存直到页面关闭。
  URL.revokeObjectURL(url)
}
