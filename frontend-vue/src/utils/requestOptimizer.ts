import axios, {  } from 'axios'
import type { AxiosRequestConfig, Canceler, AxiosInstance } from 'axios'

/**
 * 请求优化管理器
 * 
 * 功能:
 * 1. 路由切换时自动取消未完成的请求
 * 2. 相同请求短时间内的去重 (请求合并)
 * 3. 弱网模式检测
 */

class RequestManager {
  public pendingRequests: Map<string, { cancel: Canceler; config: AxiosRequestConfig }> = new Map()
  private requestCache: Map<string, { promise: Promise<unknown>; timestamp: number }> = new Map()
  private readonly CACHE_TTL = 3000 // 缓存 TTL 3秒

  /**
   * 生成请求唯一标识
   */
  private getKey(config: AxiosRequestConfig): string {
    const method = config.method?.toUpperCase() || 'GET'
    const url = config.url || ''
    const params = method === 'GET' ? JSON.stringify(config.params || {}) : ''
    return `${method}:${url}${params}`
  }

  /**
   * 添加请求到管理器
   */
  addRequest(config: AxiosRequestConfig): void {
    const key = this.getKey(config)
    const cancelSource = axios.CancelToken.source()
    
    config.cancelToken = cancelSource.token
    this.pendingRequests.set(key, { cancel: cancelSource.cancel, config })
  }

  /**
   * 移除已完成的请求
   */
  removeRequest(config: AxiosRequestConfig): void {
    const key = this.getKey(config)
    this.pendingRequests.delete(key)
  }

  /**
   * 取消特定 URL 的请求
   */
  cancelRequest(url: string): void {
    this.pendingRequests.forEach(({ cancel, config }, key) => {
      if (config.url === url) {
        cancel('Request cancelled')
        this.pendingRequests.delete(key)
      }
    })
  }

  /**
   * 取消所有请求 (路由切换时调用)
   */
  cancelAll(): void {
    this.pendingRequests.forEach(({ cancel }) => cancel('Page navigated away'))
    this.pendingRequests.clear()
  }

  /**
   * 请求去重：相同请求在 CACHE_TTL 时间内返回缓存结果
   * 只适用于 GET 请求
   */
  async deduplicateRequest<T = unknown>(config: AxiosRequestConfig): Promise<unknown> {
    // 只对 GET 请求进行去重
    if (config.method?.toUpperCase() !== 'GET') {
      return axios.request<T>(config)
    }

    const key = this.getKey(config)
    const now = Date.now()

    // 检查缓存
    const cached = this.requestCache.get(key)
    if (cached && now - cached.timestamp < this.CACHE_TTL) {
      return cached.promise
    }

    // 创建新请求并缓存
    const promise = axios.request<T>(config)
    this.requestCache.set(key, { promise, timestamp: now })

    return promise
  }

  /**
   * 清理过期的缓存
   */
  cleanupCache(): void {
    const now = Date.now()
    for (const [key, value] of this.requestCache.entries()) {
      if (now - value.timestamp > this.CACHE_TTL) {
        this.requestCache.delete(key)
      }
    }
  }

  /**
   * 获取当前待处理请求数量
   */
  get pendingCount(): number {
    return this.pendingRequests.size
  }
}

export const requestManager = new RequestManager()

/**
 * 弱网检测
 */
export function isSlowNetwork(): boolean {
  const conn = (navigator as Navigator & { connection?: { effectiveType?: string; saveData?: boolean } }).connection
  if (!conn) return false
  
  const slowTypes = ['slow-2g', '2g', '3g']
  return slowTypes.includes(conn.effectiveType) || conn.saveData
}

/**
 * Axios 拦截器集成
 * 自动注册到请求管理器
 */
export function setupRequestInterceptors(instance: AxiosInstance): void {
  const CancelToken = axios.CancelToken
  const isCancel = axios.isCancel

  // 请求拦截器：注册到管理器
  instance.interceptors.request.use(config => {
    const source = CancelToken.source()
    config.cancelToken = source.token
    requestManager.pendingRequests.set(
      `${config.method}:${config.url}`,
      { cancel: source.cancel, config }
    )
    return config
  })

  // 响应拦截器：清理已完成的请求
  instance.interceptors.response.use(
    response => {
      const key = `${response.config.method}:${response.config.url}`
      requestManager.pendingRequests.delete(key)
      return response
    },
    error => {
      if (!isCancel(error)) {
        const key = error.config ? `${error.config.method}:${error.config.url}` : null
        if (key) requestManager.pendingRequests.delete(key)
      }
      return Promise.reject(error)
    }
  )
}

/**
 * 定期清理缓存
 */
setInterval(() => {
  requestManager.cleanupCache()
}, 10000)
