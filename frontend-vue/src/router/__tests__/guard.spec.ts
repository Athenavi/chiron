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
  /**
   * 账号状态三态：
   *   true  → pending_deletion（注销冻结）
   *   false → active
   *   省略  → **不带** account_status 字段，模拟"刷新后从 localStorage 恢复"
   *           （那里只存 id/role/name），用于验证守卫会补拉 profile
   */
  frozen?: boolean
}) {
  const store = {
    user: init.role !== undefined
      ? {
          id: 'u1',
          email: 'a@b.c',
          name: 'A',
          role: init.role,
          tenant_id: 't1',
          ...(init.frozen === undefined
            ? {}
            : { account_status: init.frozen ? 'pending_deletion' : 'active' }),
        }
      : null,
    isAuthenticated: init.isAuthenticated ?? !!init.role,
    isAccountFrozen: init.frozen ?? false,
    // 与 store 的 computed 同口径：仅平台 owner 为真
    isOwner: init.role === 'owner',
    fetchProfile: mocks.fetchProfileMock,
    initToken: vi.fn().mockResolvedValue(undefined),
  }
  mocks.useAuthStoreMock.mockReturnValue(store)
  return store
}

function fakeRoute(meta: Record<string, unknown>, path = '/x') {
  return { meta, fullPath: path, path } as unknown as RouteLocationNormalized
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
      makeStore({ role: 'user', isAuthenticated: true })
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

  // ── 注销冻结期：允许登录，但只能进设置页 ──
  //
  // 产品要求是「允许注销期的账户登录，但使其仅能进入特定的页面」。
  // 服务端用 AccountDeletionGate 兜底（其余接口 403），守卫负责
  // 别让用户点进一个注定 403 的页面。
  describe('注销冻结期', () => {
    it('访问对话页时重定向到设置页', async () => {
      makeStore({ role: 'user', isAuthenticated: true, frozen: true })
      const next = fakeNext()
      await authGuard(
        fakeRoute({ requiresAuth: true }, '/chat'),
        {} as RouteLocationNormalized,
        next,
      )
      expect(next).toHaveBeenCalledWith('/profile')
    })

    it('访问设置页本身时放行（导出与撤销都在那里）', async () => {
      makeStore({ role: 'user', isAuthenticated: true, frozen: true })
      const next = fakeNext()
      await authGuard(
        fakeRoute({ requiresAuth: true }, '/profile'),
        {} as RouteLocationNormalized,
        next,
      )
      expect(next).toHaveBeenCalledWith()
    })

    it('公开页不受影响（分享页等不该被这条规则波及）', async () => {
      makeStore({ role: 'user', isAuthenticated: true, frozen: true })
      const next = fakeNext()
      await authGuard(fakeRoute({ public: true }, '/share/abc'), {} as RouteLocationNormalized, next)
      expect(next).toHaveBeenCalledWith()
    })

    it('正常账号访问对话页不受影响', async () => {
      makeStore({ role: 'user', isAuthenticated: true, frozen: false })
      const next = fakeNext()
      await authGuard(
        fakeRoute({ requiresAuth: true }, '/chat'),
        {} as RouteLocationNormalized,
        next,
      )
      expect(next).toHaveBeenCalledWith()
    })

    // 刷新后 localStorage 只恢复 {id, role, name}，没有 account_status。
    // 不补拉 profile 的话守卫认不出冻结态，会把人放行到 /chat 再撞 403。
    it('从本地恢复的用户缺少 account_status 时会补拉 profile', async () => {
      makeStore({ role: 'user', isAuthenticated: true }) // frozen 省略 ⇒ 无该字段
      const next = fakeNext()
      await authGuard(
        fakeRoute({ requiresAuth: true }, '/chat'),
        {} as RouteLocationNormalized,
        next,
      )
      expect(mocks.fetchProfileMock).toHaveBeenCalled()
    })
  })

  // ── 平台 owner 专属页 ──
  //
  // 后端这 7 个页面背后的端点全挂 `platformReadMW`（RequirePlatformOwner），
  // 租户 admin 持有 admin:read 也照样 403。菜单对 admin 已隐藏，
  // 但隐藏不等于拦截：直接敲 URL / 旧书签还能进来，所以路由层再挡一道。
  describe('requiresOwner', () => {
    it('平台 owner 放行', async () => {
      makeStore({ role: 'owner', isAuthenticated: true })
      const next = fakeNext()
      await authGuard(
        fakeRoute({ requiresAuth: true, requiresAdmin: true, requiresOwner: true }, '/admin/api-keys'),
        {} as RouteLocationNormalized,
        next,
      )
      expect(next).toHaveBeenCalledWith()
    })

    it('租户 admin 被挡回首页（不是 403 空页面）', async () => {
      makeStore({ role: 'admin', isAuthenticated: true })
      const next = fakeNext()
      await authGuard(
        fakeRoute({ requiresAuth: true, requiresAdmin: true, requiresOwner: true }, '/admin/api-keys'),
        {} as RouteLocationNormalized,
        next,
      )
      expect(next).toHaveBeenCalledWith('/')
    })

    it('未登录重定向 /login', async () => {
      makeStore({ role: null, isAuthenticated: false })
      const next = fakeNext()
      await authGuard(
        fakeRoute({ requiresAuth: true, requiresOwner: true }, '/admin/api-keys'),
        {} as RouteLocationNormalized,
        next,
      )
      expect(next).toHaveBeenCalledWith('/login')
    })

    it('admin 仍可进入非 owner 专属的 /admin 页面', async () => {
      makeStore({ role: 'admin', isAuthenticated: true })
      const next = fakeNext()
      await authGuard(
        fakeRoute({ requiresAuth: true, requiresAdmin: true }, '/admin/audit'),
        {} as RouteLocationNormalized,
        next,
      )
      expect(next).toHaveBeenCalledWith()
    })
  })
})
