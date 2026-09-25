import { ref } from 'vue'
import type { Router } from 'vue-router'

export const routeLoading = ref(false)

let timer: ReturnType<typeof setTimeout> | null = null

export function startProgress() {
  if (timer) clearTimeout(timer)
  timer = setTimeout(() => { routeLoading.value = true }, 50)
}

export function stopProgress() {
  if (timer) { clearTimeout(timer); timer = null }
  routeLoading.value = false
}

/**
 * 路由预加载器
 * 使用 IntersectionObserver 预加载可见区域外的路由
 * 在浏览器空闲时执行预加载，避免阻塞主线程
 */
export function setupRoutePreload(router: Router) {
  // 已预加载的路由缓存
  const preloadedRoutes = new Set<string>()
  
  // 获取路由组件
  const getRouteComponent = (routeName: string) => {
    const route = router.getRoutes().find(r => r.name === routeName)
    return route?.components?.default || route?.children?.[0]?.components?.default
  }
  
  // 预加载单个路由
  const preloadRoute = async (routeName: string) => {
    if (preloadedRoutes.has(routeName)) return
    preloadedRoutes.add(routeName)
    
    try {
      const component = getRouteComponent(routeName)
      if (component && typeof component === 'function') {
        const loadFn = component as () => Promise<unknown>
        await loadFn()
        if (import.meta.env.DEV) {
          console.log(`[Preload] Route ${routeName} preloaded successfully`)
        }
      }
    } catch (error) {
      console.warn(`[Preload] Failed to preload route ${routeName}:`, error)
      preloadedRoutes.delete(routeName)
    }
  }
  
  // 使用 requestIdleCallback 在空闲时预加载
  const idlePreload = (routeName: string) => {
    if ('requestIdleCallback' in window) {
      requestIdleCallback(() => preloadRoute(routeName), { timeout: 2000 })
    } else {
      setTimeout(() => preloadRoute(routeName), 100)
    }
  }
  
  // 预加载常用路由（按优先级）
  const commonRoutes = [
    'Chat',
    'Agents',
    'Skills',
    'Knowledge',
    'Plugins',
    'AdminDashboard',
  ]
  
  // 页面加载完成后延迟预加载
  window.addEventListener('load', () => {
    // 等待 3 秒确保主路由已加载
    setTimeout(() => {
      commonRoutes.forEach(route => idlePreload(route))
    }, 3000)
  })
  
  // 监听鼠标悬停预加载（用于导航菜单）
  const setupHoverPreload = () => {
    const observer = new MutationObserver(() => {
      document.querySelectorAll('[data-preload-route]').forEach(el => {
        const routeName = el.getAttribute('data-preload-route')
        if (routeName) {
          el.addEventListener('mouseenter', () => {
            idlePreload(routeName)
          }, { once: true })
        }
      })
    })
    
    observer.observe(document.body, { childList: true, subtree: true })
  }
  
  // 延迟初始化悬停预加载。
  // 不用 `requestIdleCallback?.(...) || setTimeout(...)`：handle 可能返回 0，
  // 那会让 || 判为假而再注册一次 setTimeout，同一次预加载跑两遍。
  if (typeof requestIdleCallback === 'function') {
    requestIdleCallback(setupHoverPreload, { timeout: 1000 })
  } else {
    setTimeout(setupHoverPreload, 1000)
  }
}

/** 在 router 上挂载进度钩子 */
export function setupRouteProgress(router: Router) {
  router.beforeEach((_to, from, next) => {
    startProgress()
    next()
  })
  router.afterEach(() => {
    stopProgress()
  })
  router.onError(() => {
    stopProgress()
  })
  
  // 启用路由预加载
  setupRoutePreload(router)
}
