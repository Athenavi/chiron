/**
 * 性能监控 Composable
 * 
 * 功能：
 * - Core Web Vitals 指标收集 (LCP, FID, CLS, FCP, TTFB)
 * - 路由切换性能追踪
 * - 首次渲染时间测量
 * - 网络状态监控
 * - 内存使用监控 (Chrome only)
 * 
 * 使用 Chrome DevTools Performance 面板：
 * 1. 打开 DevTools -> Performance 标签
 * 2. 录制性能剖面
 * 3. 查看自定义性能标记 (Performance > User timing)
 * 
 * 使用 Lighthouse:
 * 1. 打开 DevTools -> Lighthouse 标签
 * 2. 选择 Performance 并生成报告
 * 3. 指标会自动收集并可通过 __ ChironPerf 访问
 */

import { ref, computed } from 'vue'

// 性能数据接口
export interface PerfData {
  lcp: number | null      // Largest Contentful Paint (毫秒)
  fid: number | null      // First Input Delay (毫秒)
  cls: number | null      // Cumulative Layout Shift
  fcp: number | null      // First Contentful Paint (毫秒)
  ttfb: number | null     // Time to First Byte (毫秒)
  firstRenderMs: number   // Vue 首次渲染耗时 (毫秒)
}

// Performance Entry 类型扩展
interface ExtendedPerformanceEntry {
  processingStart?: number
  startTime: number
  hadRecentInput?: boolean
  value?: number
  responseStart?: number
  attribution?: unknown
  name: string
  entryType: string
  duration?: number
}

/**
 * 性能监控 Hook
 * 在应用初始化时调用，自动开始收集性能数据
 */
export function usePerformanceMonitor() {
  const perfData = ref<PerfData>({
    lcp: null,
    fid: null,
    cls: null,
    fcp: null,
    ttfb: null,
    firstRenderMs: 0,
  })

  const networkStatus = computed(() => {
    if (!navigator.onLine) return 'offline'
    const conn = (navigator as Navigator & { connection?: { effectiveType?: string } }).connection
    return conn?.effectiveType ?? 'unknown'
  })

  const memoryStatus = computed(() => {
    if (!('performance' in window) || !('memory' in performance)) return null
    const mem = (performance as Performance & {
      memory?: { usedJSHeapSize: number; totalJSHeapSize: number; jsHeapSizeLimit: number }
    }).memory
    if (!mem) return null
    return {
      usedJSHeapSize: mem.usedJSHeapSize,
      totalJSHeapSize: mem.totalJSHeapSize,
      jsHeapSizeLimit: mem.jsHeapSizeLimit,
      usagePercent: Math.round((mem.usedJSHeapSize / mem.jsHeapSizeLimit) * 100),
    }
  })

  /**
   * 初始化性能监控
   */
  const initMonitor = () => {
    // 1. 测量首次渲染时间
    const start = performance.now()
    const observer = new PerformanceObserver(list => {
      for (const entry of list.getEntries()) {
        if (entry.name === 'vue-first-render') {
          perfData.value.firstRenderMs = Math.round(entry.duration)
          performance.clearMarks('vue-first-render')
          observer.disconnect()
        }
      }
    })
    try {
      observer.observe({ entryTypes: ['mark'] })
    } catch {
      // PerformanceObserver 不支持
    }

    // 2. LCP - Largest Contentful Paint
    try {
      new PerformanceObserver(list => {
        const entries = list.getEntries()
        if (entries.length > 0) {
          perfData.value.lcp = Math.round(entries[entries.length - 1].startTime)
        }
      }).observe({ type: 'largest-contentful-paint', buffered: true })
    } catch { /* LCP not supported */ }

    // 3. FID - First Input Delay
    try {
      new PerformanceObserver(list => {
        for (const entry of list.getEntries() as ExtendedPerformanceEntry[]) {
          const value = Math.round((entry.processingStart ?? 0) - (entry.startTime ?? 0))
          if (perfData.value.fid === null || value < perfData.value.fid) {
            perfData.value.fid = value
          }
        }
      }).observe({ type: 'first-input', buffered: true })
    } catch { /* FID not supported */ }

    // 4. CLS - Cumulative Layout Shift
    let clsValue = 0
    try {
      new PerformanceObserver(list => {
        for (const entry of list.getEntries() as ExtendedPerformanceEntry[]) {
          if (!entry.hadRecentInput) {
            clsValue += (entry.value ?? 0)
            perfData.value.cls = clsValue
          }
        }
      }).observe({ type: 'layout-shift', buffered: true })
    } catch { /* CLS not supported */ }

    // 5. FCP - First Contentful Paint
    try {
      new PerformanceObserver(list => {
        for (const entry of list.getEntries()) {
          if (entry.entryType === 'paint' && entry.name === 'first-contentful-paint') {
            perfData.value.fcp = Math.round(entry.startTime)
          }
        }
      }).observe({ type: 'paint', buffered: true })
    } catch { /* FCP not supported */ }

    // 6. TTFB - Time to First Byte
    try {
      new PerformanceObserver(list => {
        for (const entry of list.getEntries() as ExtendedPerformanceEntry[]) {
          if (entry.entryType === 'navigation') {
            perfData.value.ttfb = Math.round(entry.responseStart ?? 0)
          }
        }
      }).observe({ type: 'navigation', buffered: true })
    } catch { /* TTFB not supported */ }

    // 7. 长任务监控 (Long Tasks)
    if (import.meta.env.DEV) {
      try {
        new PerformanceObserver(list => {
          for (const entry of list.getEntries() as ExtendedPerformanceEntry[]) {
            console.warn(
              `[Performance] Long task detected: ${Math.round(entry.duration ?? 0)}ms`,
              entry.attribution
            )
          }
        }).observe({ type: 'longtask', buffered: true })
      } catch { /* Long task observer not supported */ }
    }

    // 8. 网络状态变化监听
    const onOnline = () => console.log('[Performance] Network: online')
    const onOffline = () => console.warn('[Performance] Network: offline')
    window.addEventListener('online', onOnline)
    window.addEventListener('offline', onOffline)
  }

  /**
   * 标记性能点 (用于 DevTools Performance 面板)
   */
  const mark = (name: string) => {
    performance.mark(name)
  }

  /**
   * 测量两个标记之间的时间
   */
  const measure = (name: string, startMark: string, endMark: string) => {
    try {
      performance.measure(name, startMark, endMark)
      return performance.getEntriesByName(name)[0]?.duration ?? null
    } catch {
      return null
    }
  }

  /**
   * 获取性能报告
   */
  const getReport = (): PerfData => {
    return { ...perfData.value }
  }

  // 暴露所有数据和方法
  return {
    perfData,
    networkStatus,
    memoryStatus,
    initMonitor,
    mark,
    measure,
    getReport,
  }
}