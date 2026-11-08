import { describe, expect, it } from 'vitest'
import { routes } from '../index'

/**
 * 公开路由契约（回归测试）。
 *
 * 这些页面不能依赖登录态：一旦丢掉 `meta.public`，`App.vue` 的 `showLayout`
 * 会挂上全局布局，未登录时它发出的 `/v1/tenant/*` 请求返回 401，
 * 401 拦截器再执行 `window.location.href = '/login'`，页面重载后再次挂载 ——
 * 用户看到的就是「登录页无限刷新」。
 */
const PUBLIC_PATHS = ['/login', '/register', '/forgot-password', '/reset-password', '/share/:id']

describe('公开路由 meta.public', () => {
  it.each(PUBLIC_PATHS)('%s 必须标记 public', (path) => {
    const route = routes.find((r) => r.path === path)
    expect(route, `route ${path} 不存在`).toBeTruthy()
    expect(route?.meta?.public).toBe(true)
  })

  it('/chat 需要登录，且不能被误标为 public', () => {
    const route = routes.find((r) => r.path === '/chat')
    expect(route?.meta?.requiresAuth).toBe(true)
    expect(route?.meta?.public).toBeUndefined()
  })
})
