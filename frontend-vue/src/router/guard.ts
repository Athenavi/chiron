import type { RouteLocationNormalized, NavigationGuardNext } from 'vue-router'
import { useAuthStore } from '../stores/auth'
import { requestManager } from '../utils/requestOptimizer'

/**
 * 全局路由守卫：
 * - requiresAuth：未登录（无 user）→ 重定向 /login
 * - requiresAdmin：非 admin/owner 角色 → 重定向（登录态回首页，未登录回 /login）
 *
 * S 安全：token 已迁至 httpOnly cookie，JS 不可读。admin 权限**不得**信任
 * localStorage.role（可被用户篡改为 owner）。每次进入 admin 页都向后端拉权威身份，
 * 防止伪 admin 借守卫放行敏感管理接口。
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

  // 认证态是异步的；main.ts 通常已等待，这里兜底保证首屏请求带上 Bearer。
  await auth.initToken?.()

  // 三种情形需要向后端拉权威身份：
  //  1) admin 页 —— 角色判定不能信 localStorage（可被篡改成 owner）
  //  2) 还没有 user —— 首屏
  //  3) 有 user 但没有 account_status —— 刷新后从 localStorage 恢复的 user
  //     只有 id/role/name（见 loadUserFromStorage），缺了这个字段
  //     守卫就认不出"注销冻结中"，会把人放行到 /chat 然后再撞 403。
  //     account_status 刻意不持久化（权限依据必须来自服务端），
  //     所以每次刷新都会补拉一次 —— 这是正确性必要的代价。
  const needsAuthority = to.meta.requiresAdmin
    || (!auth.user && !to.meta.public)
    || (!!auth.user && !auth.user.account_status && !to.meta.public)
  if (needsAuthority) {
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

  // 平台 owner 专属页：与后端 `platformReadMW` / `platformWriteMW`
  // （`RequirePlatformOwner`）同口径。
  //
  // 菜单里这些项对租户 admin 已隐藏（见 views/admin/Layout.vue 的 `ownerOnly`），
  // 但**隐藏不等于拦截** —— 直接敲 URL、旧书签、外部告警链接都还能进来。
  // 服务端最终会 403，可那样用户看到的是一张空页面加报错，
  // 会误判成系统故障；这里在路由层就挡掉，语义才一致。
  if (to.meta.requiresOwner && !auth.isOwner) {
    next(auth.isAuthenticated ? '/' : '/login')
    return
  }

  // 注销冻结期：允许登录，但只放行设置页（导出数据 / 撤销注销）。
  //
  // 服务端已用 AccountDeletionGate 兜底（其余接口一律 403），这里做的是
  // 别让用户点进一个注定 403 的页面 —— 那种「能看见却什么都不通」的体验
  // 会让人以为是系统故障，而不是"我的账号正在注销中"。
  //
  // 用 meta.public 排除公开页：分享页等不该被这条规则波及。
  if (auth.isAccountFrozen && !to.meta.public && to.path !== FROZEN_ALLOWED_PATH) {
    next(FROZEN_ALLOWED_PATH)
    return
  }

  next()
}

/**
 * 冻结期唯一可停留的页面：设置页。
 *
 * 选它是因为「导出我的数据」与「撤销注销」都在这个页面的账户 tab 里 ——
 * 换句话说，这是冻结期**唯一有实际功能**的页面。
 */
export const FROZEN_ALLOWED_PATH = '/profile'
