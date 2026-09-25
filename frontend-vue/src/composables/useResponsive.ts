/**
 * 响应式断点 Composable
 * 
 * 用于检测设备类型和屏幕尺寸的 Composable
 * 支持移动端、平板端和桌面端的断点管理
 * 
 * 断点定义:
 * - 手机: 0-576px
 * - 平板: 577-768px  
 * - 桌面: 769-992px
 * - 大屏幕: 993-1200px
 * - 超大屏幕: 1201px+
 */

import { computed } from 'vue'

export type BreakpointKey = 'sm' | 'md' | 'lg' | 'xl' | 'xxl'

const breakpointsMap: Record<BreakpointKey, number> = {
  sm: 576,
  md: 768,
  lg: 992,
  xl: 1200,
  xxl: 1600,
}

function useBreakpoints() {
  const getWidth = (): number => {
    return window.innerWidth
  }
  
  const greater = (key: BreakpointKey) => {
    return computed(() => getWidth() > breakpointsMap[key])
  }
  
  const smaller = (key: BreakpointKey) => {
    return computed(() => getWidth() <= breakpointsMap[key])
  }
  
  const greaterOrEqual = (key: BreakpointKey) => {
    return computed(() => getWidth() >= breakpointsMap[key])
  }
  
  const smallerOrEqual = (key: BreakpointKey) => {
    return computed(() => getWidth() <= breakpointsMap[key])
  }
  
  const between = (start: BreakpointKey, end: BreakpointKey) => {
    return computed(() => {
      const w = getWidth()
      return w > breakpointsMap[start] && w <= breakpointsMap[end]
    })
  }
  
  return {
    greater,
    smaller,
    greaterOrEqual,
    smallerOrEqual,
    between,
  }
}

export function useResponsive() {
  const { greater, smaller, between } = useBreakpoints()
  
  // 设备类型
  const isMobile = smaller('md')
  const isTablet = between('md', 'lg')
  const isDesktop = greater('lg')
  
  // 屏幕尺寸
  const isSmall = smaller('md')
  const isMedium = between('md', 'lg')
  const isLarge = greater('lg')
  const isXLarge = greater('xl')

  // 是否为触摸设备
  const isTouchDevice = computed(() => {
    return 'ontouchstart' in window || 
           navigator.maxTouchPoints > 0 || 
           (navigator as Navigator & { msMaxTouchPoints?: number }).msMaxTouchPoints! > 0
  })

  // 是否为 Safari (iOS 需要特殊处理)
  const isSafari = computed(() => {
    const ua = navigator.userAgent
    return /Safari/.test(ua) && !/Chrome/.test(ua)
  })

  // 网络质量 (如果支持 Network Information API)
  const networkConnection = computed(() => {
    if (!('connection' in navigator)) return null
    const conn = (navigator as Navigator & { connection?: { effectiveType?: string; saveData?: boolean } }).connection
    if (!conn) return null
    return {
      effectiveType: conn.effectiveType ?? 'unknown',
      downlink: conn.downlink ?? 0,
      saveData: conn.saveData ?? false,
      rtt: conn.rtt ?? 0,
    }
  })

  // 是否为弱网
  const isSlowNetwork = computed(() => {
    if (!networkConnection.value) return false
    const conn = networkConnection.value
    return conn.effectiveType === 'slow-2g' || 
           conn.effectiveType === '2g' || 
           conn.effectiveType === '3g' ||
           conn.saveData
  })

  // 动画性能策略
  const animationStrategy = computed(() => {
    if (isMobile.value) return 'reduced'
    if (isSlowNetwork.value) return 'minimal'
    if (isDesktop.value) return 'full'
    return 'standard'
  })

  // 网格列数 (响应式)
  const gridColumns = computed(() => {
    if (isMobile.value) return 1
    if (isTablet.value) return 2
    if (isLarge.value) return 3
    return 4
  })

  return {
    // 设备类型
    isMobile,
    isTablet,
    isDesktop,
    
    // 屏幕尺寸
    isSmall,
    isMedium,
    isLarge,
    isXLarge,
    
    // 设备特性
    isTouchDevice,
    isSafari,
    networkConnection,
    isSlowNetwork,
    
    // 性能策略
    animationStrategy,
    
    // 响应式布局
    gridColumns,
    
    // 断点检测函数
    greater,
    smaller,
    between,
  }
}