import { defineStore } from 'pinia'
import { ref, computed } from 'vue'
import { api, setAuthToken } from '../api'
import { clearPrivateStorage } from '../utils/privateStorage'
import { applyServerLocale } from '../utils/localeSync'

export interface User {
  id: string
  email: string
  name: string
  role: string
  tenant_id: number
  /**
   * 账号状态：active / pending_deletion / purged。
   *
   * 来自 /v1/auth/profile（权威）与登录响应。注销冻结期的账号**可以登录**，
   * 但只能进设置页做两件事：导出数据、撤销注销。路由守卫据此收窄可访问页面。
   *
   * 刻意**不持久化**到 localStorage：这是权限依据，必须每次从服务端取，
   * 落盘等于给了本地篡改的机会。
   */
  account_status?: string
  /** 用户设置（换肤、朗读音色等）。由 GET /v1/auth/profile 下发、PUT 局部合并。 */
  settings?: Record<string, unknown>
}

// S 安全修复：仅持久化最小必要字段（id, role, name），不含 email/tenant_id 等敏感信息
interface UserStored {
  id: string
  role: string
  name: string
}

function loadUserFromStorage(): User | null {
  try {
    const raw = localStorage.getItem('user')
    if (!raw) return null
    const stored = JSON.parse(raw) as UserStored
    // 从存储恢复最小字段，其余字段由后端 profile 填充
    return { id: stored.id, role: stored.role, name: stored.name, email: '', tenant_id: 0 }
  } catch {
    return null
  }
}

function persistUserToStorage(u: User | null) {
  if (u) {
    const stored: UserStored = { id: u.id, role: u.role, name: u.name }
    localStorage.setItem('user', JSON.stringify(stored))
  } else {
    localStorage.removeItem('user')
  }
}

export const useAuthStore = defineStore('auth', () => {
  const user = ref<User | null>(loadUserFromStorage())
  // S 安全：token 不再持久化到 localStorage（XSS 偷不走），仅保留内存态；
  // 同源请求主要靠 httpOnly cookie，内存里的 Bearer 供 SSO 回跳等场景使用。
  const token = ref<string>('')
  let tokenInit: Promise<void> | null = null

  /**
   * MFA 两步登录的中间态：密码通过后后端返回的短期挑战令牌。
   *
   * 只存在内存里，**不持久化**：它 5 分钟就过期，落盘没有任何收益，
   * 反而多一个可被读取的凭据副本。
   */
  const mfaChallenge = ref<string>('')

  /** 统一写入内存 token 与请求层 Bearer */
  function applyToken(t: string) {
    token.value = t
    setAuthToken(t)
  }

  /**
   * 恢复上次会话的 token。
   *
   * 当前实现无持久化来源（token 只存在内存），保留这个入口是为了让启动序列
   * （`main.ts`）与路由守卫有一个"认证态就绪"的 await 点。
   */
  async function initToken(): Promise<void> {
    if (!tokenInit) tokenInit = Promise.resolve()
    return tokenInit
  }

  const loading = ref(false)

  // 有 user 才算已登录（权威身份来自 GET /v1/auth/profile）
  const isAuthenticated = computed(() => !!user.value)
  // S 修复：owner 角色拥有全部 admin 权限，isAdmin 须同时识别 admin + owner
  const isAdmin = computed(() => user.value?.role === 'admin' || user.value?.role === 'owner')
  /**
   * 是否为**平台级** owner —— 与后端 `isPlatformOwner` 同口径（`role === 'owner'`）。
   *
   * 存在的理由：`/admin` 后台里有一大批端点挂的是 `platformReadMW`
   * （`RequirePlatformOwner`），租户 admin 持有 `admin:read` 也照样 403。
   * 前端拿这个判断来决定**菜单渲不渲染**，避免"能看见、点进去必 403"。
   *
   * 注意它只管渲染，不是权限闸门 —— 真正的拦截在服务端，
   * 这里的 role 也只是服务端 profile 的镜像，不持久化、不可篡改。
   */
  const isOwner = computed(() => user.value?.role === 'owner')
  /**
   * 账号是否处于注销冻结期（已申请注销、尚未到期清除）。
   *
   * 冻结期**允许登录**，但只能进设置页（导出数据 / 撤销注销）。
   * 服务端有 AccountDeletionGate 兜底（其余接口一律 403），
   * 这里的作用是别让用户点进一个注定 403 的页面。
   *
   * 空值按 false：老响应没有该字段，且 profile 读不到时会回退 active。
   */
  const isAccountFrozen = computed(() => user.value?.account_status === 'pending_deletion')

  async function login(email: string, password: string, captcha?: { token: string; randstr?: string }) {
    loading.value = true
    try {
      const response = await api.post('/v1/auth/login', {
        email,
        password,
        captcha_token: captcha?.token || '',
        captcha_randstr: captcha?.randstr || '',
      })
      const d = response.data?.data
      if (!d) throw new Error('invalid login response')

      // 两步登录：开启双因子时后端**不签发**正式 token，只返回一个短期挑战令牌
      // （独立密钥签名，无法当会话令牌用）。此时绝不能 applyToken —— 那会把
      // 空值写进请求层，用户被静默卡在登录页且没有任何错误提示。
      // 返回 mfaRequired 让登录页切到"输入验证码"这一步。
      if (d.mfa_required) {
        mfaChallenge.value = d.challenge_token || ''
        return { mfaRequired: true as const }
      }

      // Web 走 httpOnly cookie；桌面端 file:// 跨站拿不到 cookie，applyToken 同时
      // 把 token 交给请求层走 Bearer（仅桌面端持久化）。
      applyToken(d.token)
      user.value = d.user
      persistUserToStorage(d.user)
      mfaChallenge.value = ''
      // 保留原有的响应字段（token/user），只额外附加 mfaRequired ——
      // 既有调用方与测试仍在读 d.token，改结构会平白破坏它们。
      return { ...d, mfaRequired: false as const }
    } finally {
      loading.value = false
    }
  }

  /**
   * 登录第二步：用挑战令牌 + 验证码换取正式 token。
   *
   * 验证码与备用码共用同一个输入框：用户不会区分"我现在输的是哪种"。
   */
  async function completeMfaLogin(code: string) {
    loading.value = true
    try {
      const response = await api.post('/v1/auth/mfa/verify', {
        challenge_token: mfaChallenge.value,
        code,
      })
      const d = response.data?.data
      if (!d?.token || !d?.user) throw new Error('invalid mfa response')
      applyToken(d.token)
      user.value = d.user
      persistUserToStorage(d.user)
      mfaChallenge.value = ''
      return d.user as User
    } finally {
      loading.value = false
    }
  }

  async function register(
    email: string,
    password: string,
    name: string,
    captcha?: { token: string; randstr?: string },
    emailCode?: string,
    inviteCode?: string,
  ) {
    loading.value = true
    try {
      const response = await api.post('/v1/auth/register', {
        email,
        password,
        name,
        // 后台开启「注册邮箱验证」时必填，未开启时后端忽略
        email_code: emailCode || '',
        // 邀请码加入指定租户（后端在校验失败时拒绝注册）
        invite_code: inviteCode || '',
        captcha_token: captcha?.token || '',
        captcha_randstr: captcha?.randstr || '',
      })
      const d = response.data?.data
      if (!d) throw new Error('invalid register response')
      applyToken(d.token)
      user.value = d.user
      persistUserToStorage(d.user)
      return d
    } finally {
      loading.value = false
    }
  }

  async function fetchProfile() {
    if (!user.value) return
    try {
      const response = await api.get('/v1/auth/profile')
      const data = response.data?.data
      if (!data) throw new Error('invalid profile response')
      user.value = {
        id: data.user_id,
        email: data.email,
        name: data.name || data.email,
        role: data.role,
        tenant_id: data.tenant_id || 0,
        // 权限依据必须来自服务端；守卫据此把冻结期账号限制在设置页。
        account_status: data.account_status,
        settings: data.settings,
      }
      persistUserToStorage(user.value)
      // 按服务端语言偏好对齐界面（仅在本机未显式选择过语言时生效）。
      // 服务端渲染的通知邮件只能读 users.settings.locale，这里同步是为了让
      // 「不同设备上语言一致」，而非覆盖用户当下的选择。
      applyServerLocale(data.settings)
    } catch {
      // Cookie 无效，清除本地态
      await logout()
    }
  }

  /** SSO 回跳引导：凭 httpOnly cookie 换取会话信息（SSO 登录后建立本地 user 态） */
  async function bootstrapSession(): Promise<boolean> {
    try {
      const response = await api.get('/v1/auth/session', { withCredentials: true })
      const d = response.data?.data
      if (!d?.token) return false
      // 桌面端同样走 Bearer：SSO 回跳的 cookie 在 file:// 下也不会被带上
      applyToken(d.token)
      user.value = d.user
      persistUserToStorage(d.user)
      return true
    } catch {
      return false
    }
  }

  /** 建立/覆盖本地会话（短信登录等直接拿到 {token, user} 的入口复用） */
  function applySession(t: string, u: User) {
    if (!t) throw new Error('invalid session: empty token')
    applyToken(t)
    user.value = u
    persistUserToStorage(u)
  }

  // S 安全：logout 调后端清 httpOnly cookie（否则登出后 cookie 仍有效，鉴权不失效）
  async function logout() {
    try {
      await api.post('/v1/auth/logout')
    } catch {
      // 网络失败仍清理本地态
    }
    applyToken('')
    user.value = null
    persistUserToStorage(null)
    // 私有数据（输入历史、会话缓存…）必须随登出清掉：localStorage 是浏览器级的，
    // 不清就会在下一次登录时被**另一个账号**读到（实测：输入历史 `↑` 召回了他人内容）。
    clearPrivateStorage()
  }

  return {
    user,
    token,
    loading,
    isAuthenticated,
    isAdmin,
    isOwner,
    isAccountFrozen,
    mfaChallenge,
    initToken,
    login,
    completeMfaLogin,
    register,
    fetchProfile,
    bootstrapSession,
    applySession,
    logout,
  }
})
