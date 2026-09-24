import { describe, it, expect, vi, beforeEach } from 'vitest'
import { setActivePinia, createPinia } from 'pinia'

vi.mock('../../api', () => ({
  api: {
    post: vi.fn(),
    get: vi.fn(),
  },
}))

import { useAuthStore } from '../auth'
import { api } from '../../api'

const mockUser = { id: 'u1', email: 'a@b.c', name: 'A', role: 'admin', tenant_id: 't1' }

/**
 * localStorage 里**只允许**出现最小字段。
 * email / tenant_id 属敏感信息，被 S 安全修复刻意排除（见 src/stores/auth.ts 的
 * persistUserToStorage / loadUserFromStorage）：刷新后由 GET /v1/auth/profile 回填。
 */
const mockUserStored = { id: 'u1', name: 'A', role: 'admin' }

describe('auth store', () => {
  beforeEach(() => {
    setActivePinia(createPinia())
    // jsdom 30 兼容：localStorage 可能未正确初始化
    try { localStorage.clear() } catch (e) {
      // Ignore localStorage errors in test environment
      void e
    }
    vi.clearAllMocks()
  })

  describe('login', () => {
    it('成功后持久化 user（token 不进 localStorage，凭 httpOnly cookie）', async () => {
      vi.mocked(api.post).mockResolvedValue({ data: { data: { token: 'tok', user: mockUser } } })
      const store = useAuthStore()
      const d = await store.login('a@b.c', 'password')
      expect(d.token).toBe('tok')
      expect(store.token).toBe('tok')
      expect(store.user).toEqual(mockUser)
      // S 安全：token 不写入 localStorage（凭 httpOnly cookie 鉴权）
      expect(localStorage.getItem('token')).toBeNull()
      expect(JSON.parse(localStorage.getItem('user') || 'null')).toEqual(mockUserStored)
      // 请求体带 captcha 字段（空串兜底）
      expect(vi.mocked(api.post)).toHaveBeenCalledWith('/v1/auth/login', {
        email: 'a@b.c',
        password: 'password',
        captcha_token: '',
        captcha_randstr: '',
      })
    })

    it('携带验证码参数时透传到请求体', async () => {
      vi.mocked(api.post).mockResolvedValue({ data: { data: { token: 'tok', user: mockUser } } })
      const store = useAuthStore()
      await store.login('a@b.c', 'password', { token: 'cap-token', randstr: 'rnd' })
      expect(vi.mocked(api.post)).toHaveBeenCalledWith('/v1/auth/login', {
        email: 'a@b.c',
        password: 'password',
        captcha_token: 'cap-token',
        captcha_randstr: 'rnd',
      })
    })

    it('响应缺 data 时抛错且不写入状态', async () => {
      vi.mocked(api.post).mockResolvedValue({ data: {} })
      const store = useAuthStore()
      await expect(store.login('a@b.c', 'password')).rejects.toThrow('invalid login response')
      expect(store.token).toBe('')
      expect(store.user).toBeNull()
      expect(localStorage.getItem('user')).toBeNull()
    })

    it('loading 在失败时也复位', async () => {
      vi.mocked(api.post).mockRejectedValue(new Error('network'))
      const store = useAuthStore()
      await expect(store.login('a@b.c', 'password')).rejects.toThrow('network')
      expect(store.loading).toBe(false)
    })
  })

  describe('register', () => {
    it('成功后持久化 user（token 不进 localStorage）', async () => {
      const user = { ...mockUser, role: 'user' }
      vi.mocked(api.post).mockResolvedValue({ data: { data: { token: 'rtok', user } } })
      const store = useAuthStore()
      await store.register('a@b.c', 'password', 'Name')
      expect(store.token).toBe('rtok')
      expect(localStorage.getItem('token')).toBeNull()
      expect(JSON.parse(localStorage.getItem('user') || 'null')).toEqual(
        { id: user.id, name: user.name, role: user.role },
      )
    })
  })

  describe('isAdmin', () => {
    it('admin 角色为 true', () => {
      localStorage.setItem('user', JSON.stringify(mockUser))
      const store = useAuthStore()
      expect(store.isAdmin).toBe(true)
    })

    it('普通角色为 false', () => {
      localStorage.setItem('user', JSON.stringify({ ...mockUser, role: 'user' }))
      const store = useAuthStore()
      expect(store.isAdmin).toBe(false)
    })

    it('无 user 为 false', () => {
      const store = useAuthStore()
      expect(store.isAdmin).toBe(false)
    })
  })

  describe('store 初始化', () => {
    it('从 localStorage 恢复 user（页面刷新场景；token 内存态为空但 isAuthenticated 凭 user 为 true）', () => {
      localStorage.setItem('user', JSON.stringify(mockUser))
      const store = useAuthStore()
      expect(store.token).toBe('')
      // 恢复时 email / tenant_id 为空 —— 它们不落 localStorage，靠 /v1/auth/profile 回填
      expect(store.user).toEqual({ ...mockUserStored, email: '', tenant_id: '' })
      expect(store.isAuthenticated).toBe(true)
    })

    it('user localStorage 损坏时优雅降级为 null', () => {
      localStorage.setItem('user', '{broken')
      const store = useAuthStore()
      expect(store.user).toBeNull()
    })
  })

  describe('fetchProfile', () => {
    it('成功后更新并持久化 user', async () => {
      localStorage.setItem('user', JSON.stringify(mockUser))
      vi.mocked(api.get).mockResolvedValue({
        data: { data: { user_id: 'u2', email: 'x@y.z', name: 'X', role: 'user', tenant_id: 't2' } },
      })
      const store = useAuthStore()
      await store.fetchProfile()
      expect(store.user).toEqual({ id: 'u2', email: 'x@y.z', name: 'X', role: 'user', tenant_id: 't2' })
      expect(JSON.parse(localStorage.getItem('user') || 'null')).toEqual({ id: 'u2', name: 'X', role: 'user' })
    })

    it('失败（cookie 无效）时自动登出并清理本地态', async () => {
      localStorage.setItem('user', JSON.stringify(mockUser))
      vi.mocked(api.get).mockRejectedValue(new Error('401'))
      // logout 现调后端清 cookie
      vi.mocked(api.post).mockResolvedValue({ data: {} })
      const store = useAuthStore()
      await store.fetchProfile()
      expect(store.token).toBe('')
      expect(store.user).toBeNull()
      expect(localStorage.getItem('user')).toBeNull()
    })

    it('无 user 时直接跳过（刷新后未登录）', async () => {
      const store = useAuthStore()
      await store.fetchProfile()
      expect(api.get).not.toHaveBeenCalled()
    })
  })

  describe('bootstrapSession', () => {
    it('SSO cookie 引导成功后持久化 user（token 不进 localStorage）', async () => {
      vi.mocked(api.get).mockResolvedValue({ data: { data: { token: 'sso-tok', user: mockUser } } })
      const store = useAuthStore()
      const ok = await store.bootstrapSession()
      expect(ok).toBe(true)
      expect(store.token).toBe('sso-tok')
      expect(localStorage.getItem('token')).toBeNull()
      expect(JSON.parse(localStorage.getItem('user') || 'null')).toEqual(mockUserStored)
      // withCredentials: true（跨端口携带 httpOnly cookie）
      expect(vi.mocked(api.get)).toHaveBeenCalledWith('/v1/auth/session', { withCredentials: true })
    })

    it('响应无 token 时返回 false', async () => {
      vi.mocked(api.get).mockResolvedValue({ data: { data: {} } })
      const store = useAuthStore()
      const ok = await store.bootstrapSession()
      expect(ok).toBe(false)
    })

    it('请求失败时返回 false 不抛错', async () => {
      vi.mocked(api.get).mockRejectedValue(new Error('network'))
      const store = useAuthStore()
      const ok = await store.bootstrapSession()
      expect(ok).toBe(false)
    })
  })

  describe('logout', () => {
    it('调后端清 httpOnly cookie 并清理本地 user 态', async () => {
      localStorage.setItem('user', JSON.stringify(mockUser))
      vi.mocked(api.post).mockResolvedValue({ data: {} })
      const store = useAuthStore()
      await store.logout()
      expect(vi.mocked(api.post)).toHaveBeenCalledWith('/v1/auth/logout')
      expect(store.token).toBe('')
      expect(store.user).toBeNull()
      expect(localStorage.getItem('user')).toBeNull()
    })

    it('后端调用失败仍清理本地态（不阻塞登出）', async () => {
      localStorage.setItem('user', JSON.stringify(mockUser))
      vi.mocked(api.post).mockRejectedValue(new Error('network'))
      const store = useAuthStore()
      await store.logout()
      expect(store.token).toBe('')
      expect(store.user).toBeNull()
      expect(localStorage.getItem('user')).toBeNull()
    })
  })
})
