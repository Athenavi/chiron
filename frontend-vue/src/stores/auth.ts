import { defineStore } from 'pinia'
import { ref, computed } from 'vue'
import { api, loadDesktopToken, setAuthToken } from '../api'
import { clearPrivateStorage } from '../utils/privateStorage'
import { useDeviceStore } from './device'

export interface User {
  id: string
  email: string
  name: string
  role: string
  tenant_id: number
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
  /**
   * 运行模式：本地模式没有登录流程（本地后端的第一批只给一个固定的本地用户），
   * 但它**仍然是**一个会话 —— 界面需要 `user` 来显示名字/角色、需要 token 走 Bearer。
   */
  const device = useDeviceStore()
  const isLocalMode = () => device.mode === 'local'

  const user = ref<User | null>(loadUserFromStorage())
  // S 安全：Web 端 token 不再持久化到 localStorage（XSS 偷不走），仅保留内存态；
  // 桌面端 file:// 跨站拿不到 SameSite=Lax cookie，必须持久化 token 走 Bearer。
  const token = ref<string>('')
  let tokenInit: Promise<void> | null = null

  /** 统一写入内存 token 与请求层 Bearer（桌面端同时持久化，刷新后仍可用） */
  function applyToken(t: string) {
    token.value = t
    setAuthToken(t)
  }

  /**
   * 桌面端 token 由 safeStorage 异步解密。main.ts 在挂载前 await，路由守卫再兜底；
   * 幂等，多次调用只读一次加密文件。Web 端立即返回。
   */
  async function initToken(): Promise<void> {
    if (!tokenInit) {
      tokenInit = (async () => {
        // 本地模式：优先建立**本地会话**（不读云端 token —— 那份 token 属于另一个后端，
        // 带过去只会 401/501）。
        //
        // 这里刻意**不 await**：main.ts 会等 initToken 再 mount，而本地后端可能还在启动，
        // 一个挂住的请求能让窗口迟迟不出第一帧。本地后端第一批允许无鉴权调用，会话稍后
        // 到达即可（user / token 都是响应式的）。
        if (isLocalMode()) {
          void ensureLocalSession()
          return
        }
        const stored = await loadDesktopToken()
        if (stored) applyToken(stored)
      })()
    }
    return tokenInit
  }

  /**
   * 本地会话：`POST /v1/auth/session` 换本地用户 + 设备 token（contracts.md §4）。
   *
   * 幂等：已有 user + token 时直接返回 true。失败**不抛错** —— 本地后端可能还没
   * 启动完（首启 / 重启窗口），把启动流程拖死在 token 初始化里不值得；
   * 下一次 `initToken` / 设置页可见时会重试。
   */
  async function ensureLocalSession(force = false): Promise<boolean> {
    if (!force && user.value && token.value) return true
    try {
      const response = await api.post('/v1/auth/session')
      const d = response.data?.data
      if (!d?.token || !d?.user) return false
      applyToken(d.token)
      user.value = d.user
      persistUserToStorage(d.user)
      return true
    } catch {
      return false
    }
  }

  const loading = ref(false)

  // 本地模式：没有"登录"这回事，界面一律视为已登录（否则每个 requiresAuth 页都会弹回登录页，
  // 而登录页本身在本地模式是被拦截的云专属路由）。
  const isAuthenticated = computed(() => (isLocalMode() ? true : !!user.value))
  // S 修复：owner 角色拥有全部 admin 权限，isAdmin 须同时识别 admin + owner
  const isAdmin = computed(() => user.value?.role === 'admin' || user.value?.role === 'owner')

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
      // Web 走 httpOnly cookie；桌面端 file:// 跨站拿不到 cookie，applyToken 同时
      // 把 token 交给请求层走 Bearer（仅桌面端持久化）。
      applyToken(d.token)
      user.value = d.user
      persistUserToStorage(d.user)
      return d
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
  ) {
    loading.value = true
    try {
      const response = await api.post('/v1/auth/register', {
        email,
        password,
        name,
        // 后台开启「注册邮箱验证」时必填，未开启时后端忽略
        email_code: emailCode || '',
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
    // 本地模式：权威身份就是本地会话（也没有 /v1/auth/profile）
    if (isLocalMode()) {
      await ensureLocalSession()
      return
    }
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
      }
      persistUserToStorage(user.value)
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
    // 本地模式没有账号可登出：清掉内存态后立刻重建同一个本地会话，
    // 避免界面停在一个"名字为空、token 为空"的半登录态。
    if (isLocalMode()) {
      applyToken('')
      user.value = null
      persistUserToStorage(null)
      await ensureLocalSession(true)
      return
    }
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
    initToken,
    ensureLocalSession,
    login,
    register,
    fetchProfile,
    bootstrapSession,
    applySession,
    logout,
  }
})
