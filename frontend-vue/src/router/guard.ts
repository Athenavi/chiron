import type { RouteLocationNormalized, NavigationGuardNext } from 'vue-router'
import { useAuthStore } from '../stores/auth'
import { currentDeviceMode } from '../stores/device'
import { requestManager } from '../utils/requestOptimizer'

/**
 * 全局路由守卫：
 * - requiresAuth：未登录（无 user）→ 重定向 /login
 * - requiresAdmin：非 admin/owner 角色 → 重定向（登录态回首页，未登录回 /login）
 * - cloudOnly：云专属页面（登录/注册/计费/模型配置/管理后台）→ 本地模式下回首页
 *
 * S 安全：token 已迁至 httpOnly cookie，JS 不可读。admin 权限**不得**信任
 * localStorage.role（可被用户篡改为 owner）。每次进入 admin 页都向后端
 * /v1/auth/profile 拉取权威角色，防止伪 admin 借守卫放行敏感管理接口。
 *
 * 双模式（docs/desktop-local/contracts.md §1）：
 * 本地模式没有账号体系（本地用户由 `POST /v1/auth/session` 直接下发），因此
 * **不拉 profile、不拦 requiresAuth**；云专属路由由 `meta.cloudOnly` 统一拦截 ——
 * 路由本身不删，只是本地模式下不可达（书签/深链也不会落到 501 页面）。
 */
export async function authGuard(
  to: RouteLocationNormalized,
  _from: RouteLocationNormalized,
  next: NavigationGuardNext,
) {
  // 路由切换时取消进行中的请求，避免接口瀑布和内存泄漏
  if (to.path !== _from.path) {
    requestManager.cancelAll()
  }

  const auth = useAuthStore()

  // 桌面端 token 解密是异步的；main.ts 通常已等待，这里兜底保证首屏请求带上 Bearer。
  // 本地模式下这一步建立的是本地会话（见 stores/auth.ts 的 initToken / ensureLocalSession）。
  await auth.initToken?.()

  const localMode = currentDeviceMode() === 'local'

  if (localMode) {
    // 云专属页面（登录/注册/忘记密码/计费/模型配置/管理后台）在本地模式不存在对应后端，
    // 放进去只会得到 501 页面。统一回首页，而不是让用户对着空白页怀疑装坏了。
    if (to.meta?.cloudOnly) {
      next('/')
      return
    }
    // 本地模式恒为"已登录"（isAuthenticated 就是这么定的），requiresAuth 无需再判；
    // 管理员路由已被 cloudOnly 拦在上面，这里不再拉 profile（本地没有该端点）。
    next()
    return
  }

  // admin 访问（或本地无会话态）时，以 httpOnly cookie 向后端拉取权威 profile。
  // fetchProfile 内部失败会登出；此处不抛错，继续按现有本地态降级判定。
  // meta.public 的页面不需要登录态，跳过 profile 拉取（如分享页）。
  if (to.meta.requiresAdmin || (!auth.user && !to.meta.public)) {
    try {
      await auth.fetchProfile()
    } catch {
      /* fetchProfile 已内部登出 */
    }
  }

  if (to.meta.requiresAuth && !auth.isAuthenticated) {
    next('/login')
    return
  }

  if (to.meta.requiresAdmin) {
    const role = auth.user?.role
    if (role !== 'admin' && role !== 'owner') {
      next(auth.isAuthenticated ? '/' : '/login')
      return
    }
  }

  next()
}
