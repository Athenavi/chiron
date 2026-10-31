import type { RouteLocationNormalized, NavigationGuardNext } from 'vue-router'
import { useAuthStore } from '../stores/auth'
import { currentDeviceMode } from '../stores/device'
import { requestManager } from '../utils/requestOptimizer'

/**
 * 全局路由守卫：
 * - requiresAuth：未登录（无 user）→ 重定向 /login
 * - requiresAdmin：非 admin/owner 角色 → 重定向（登录态回首页，未登录回 /login）
 * - cloudOnly：只有云端存在的页面（登录/注册/计费）→ 本地模式下回首页
 *
 * S 安全：token 已迁至 httpOnly cookie，JS 不可读。admin 权限**不得**信任
 * localStorage.role（可被用户篡改为 owner）。每次进入 admin 页都向后端拉权威身份，
 * 防止伪 admin 借守卫放行敏感管理接口。
 *
 * 双模式（docs/desktop-local/contracts.md §1）：本地模式的服务端就是同一个 Go 网关，
 * 只是运行位置在本机，身份由 `POST /v1/auth/session` 免口令下发（真实 user + 真实
 * JWT，见 internal/api/auth_local_session.go）。因此**判定逻辑与云端共用同一条路径**，
 * 差别只有两点：
 *   1. 本地没有登录页，`isAuthenticated` 在本地模式恒真（stores/auth.ts），所以
 *      requiresAuth 失败不会把人弹到 /login 再弹回来；
 *   2. `fetchProfile` 在本地模式打的是会话端点而非 /v1/auth/profile（core 链路没有
 *      那条端点），角色仍然来自后端，不是本地存储。
 * 云专属路由由 `meta.cloudOnly` 统一拦截 —— 路由本身不删，只是本地模式下不可达
 *（书签/深链也不会落到 501 页面）。管理后台**不再标 cloudOnly**：本地模式的账户
 * 就该进得去，否则模型密钥、审计、运行时监控在单机上全都无从配置。
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

  // 云专属页面（登录/注册/忘记密码/计费）在本地模式不存在对应后端，
  // 放进去只会得到 501 页面。统一回首页，而不是让用户对着空白页怀疑装坏了。
  if (localMode && to.meta?.cloudOnly) {
    next('/')
    return
  }

  // admin 访问（或还没有 user）时向后端拉取权威身份。
  // fetchProfile 内部失败会登出；此处不抛错，继续按现有本地态降级判定。
  // meta.public 的页面不需要登录态，跳过拉取（如分享页）。
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
