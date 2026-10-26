import { describe, it, expect, vi, beforeEach } from 'vitest'
import { authGuard } from '../guard'
import type { RouteLocationNormalized, NavigationGuardNext } from 'vue-router'

// mock 掉 pinia store：守卫通过 useAuthStore() 读取会话态
const mocks = vi.hoisted(() => ({
  fetchProfileMock: vi.fn(),
  useAuthStoreMock: vi.fn(),
}))

vi.mock('../../stores/auth', () => ({
  useAuthStore: () => mocks.useAuthStoreMock(),
}))

function makeStore(init: {
  role?: string | null
  isAuthenticated?: boolean
}) {
  const store = {
    user: init.role !== undefined ? { id: 'u1', email: 'a@b.c', name: 'A', role: init.role, tenant_id: 't1' } : null,
    isAuthenticated: init.isAuthenticated ?? !!init.role,
    fetchProfile: mocks.fetchProfileMock,
    initToken: vi.fn().mockResolvedValue(undefined),
  }
  mocks.useAuthStoreMock.mockReturnValue(store)
  return store
}

function fakeRoute(meta: Record<string, unknown>) {
  return { meta, fullPath: '/x' } as unknown as RouteLocationNormalized
}

function fakeNext() {
  const next = vi.fn() as NavigationGuardNext
  return next
}

describe('authGuard', () => {
  beforeEach(() => {
    localStorage.clear()
    mocks.fetchProfileMock.mockReset()
    mocks.fetchProfileMock.mockResolvedValue(undefined)
  })

  it('requiresAuth 路由无 user 时重定向 /login', async () => {
    makeStore({ role: null, isAuthenticated: false })
    const next = fakeNext()
    await authGuard(fakeRoute({ requiresAuth: true }), {} as RouteLocationNormalized, next)
    expect(next).toHaveBeenCalledWith('/login')
  })

  it('requiresAuth 路由有 user 时放行', async () => {
    makeStore({ role: 'user', isAuthenticated: true })
    const next = fakeNext()
    await authGuard(fakeRoute({ requiresAuth: true }), {} as RouteLocationNormalized, next)
    expect(next).toHaveBeenCalledWith()
  })

  it('公开路由无 user 放行', async () => {
    makeStore({ role: null, isAuthenticated: false })
    const next = fakeNext()
    await authGuard(fakeRoute({}), {} as RouteLocationNormalized, next)
    expect(next).toHaveBeenCalledWith()
  })

  describe('requiresAdmin', () => {
    it('后端 profile 返回 admin 放行', async () => {
      const store = makeStore({ role: 'user' }) // localStorage 为 user
      // 模拟后端权威 profile 返回 admin -> 覆盖本地
      store.fetchProfile.mockImplementation(() => {
        store.user = { id: 'u1', email: 'a@b.c', name: 'A', role: 'admin', tenant_id: 't1' }
        store.isAuthenticated = true
        return Promise.resolve(undefined)
      })
      const next = fakeNext()
      await authGuard(fakeRoute({ requiresAuth: true, requiresAdmin: true }), {} as RouteLocationNormalized, next)
      expect(next).toHaveBeenCalledWith()
    })

    it('本地 role 被篡改为 owner,但后端返回普通 user 时拒绝放行(S 修复)', async () => {
      const store = makeStore({ role: 'owner' }) // 本地被篡改为 owner
      store.fetchProfile.mockImplementation(() => {
        store.user = { id: 'u1', email: 'a@b.c', name: 'A', role: 'user', tenant_id: 't1' }
        store.isAuthenticated = true
        return Promise.resolve(undefined)
      })
      const next = fakeNext()
      await authGuard(fakeRoute({ requiresAuth: true, requiresAdmin: true }), {} as RouteLocationNormalized, next)
      expect(next).toHaveBeenCalledWith('/')
    })

    it('普通用户（已登录）重定向首页 /', async () => {
      const store = makeStore({ role: 'user', isAuthenticated: true })
      const next = fakeNext()
      await authGuard(fakeRoute({ requiresAuth: true, requiresAdmin: true }), {} as RouteLocationNormalized, next)
      expect(next).toHaveBeenCalledWith('/')
    })

    it('未登录（无 user）重定向 /login', async () => {
      makeStore({ role: null, isAuthenticated: false })
      const next = fakeNext()
      await authGuard(fakeRoute({ requiresAuth: true, requiresAdmin: true }), {} as RouteLocationNormalized, next)
      expect(next).toHaveBeenCalledWith('/login')
    })

    it('backend profile 拉取失败时按本地态降级(绝不误放行 admin)', async () => {
      const store = makeStore({ role: 'user', isAuthenticated: true })
      store.fetchProfile.mockRejectedValue(new Error('network'))
      const next = fakeNext()
      await authGuard(fakeRoute({ requiresAuth: true, requiresAdmin: true }), {} as RouteLocationNormalized, next)
      expect(next).toHaveBeenCalledWith('/')
    })

    it('非 admin 路由不受 user 角色影响', async () => {
      makeStore({ role: 'user', isAuthenticated: true })
      const next = fakeNext()
      await authGuard(fakeRoute({ requiresAuth: true }), {} as RouteLocationNormalized, next)
      expect(next).toHaveBeenCalledWith()
    })
  })
})