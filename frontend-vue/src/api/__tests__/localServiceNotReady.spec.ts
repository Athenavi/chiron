import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

/**
 * 本地模式的 503 ≠ 云端限流。
 *
 * 回归背景：`desktop-local` 在存储/推理未就绪时按契约如实回 **503**
 *（`app/api/chat.py::_http_error`、`app/api/health.py::readyz`），而
 * `rateLimitBackoff.ts::isRateLimitStatus` 把 503 当限流 ⇒ 本地模式下每个依赖存储的
 * 页面都先白等 5+10+20 秒（云端的指数退避），最后被报成"服务繁忙，请稍后再试"，
 * 后端 `detail` 里的真实原因从未显示。
 *
 * 这里钉住四件事：
 * 1. 本地 503 走**短间隔**重试，而不是云端那套几十秒的退避；
 * 2. 重试用尽后文案是 `local_service_not_ready`，**不是** `server_busy_*`；
 * 3. 后端 `detail` 原文必须带出（那是定位问题的唯一线索）；
 * 4. 云端模式、以及服务端显式 `retryable: true` 时，行为保持原样。
 */

type Host = {
  isDesktop?: boolean
  mode?: 'local' | 'cloud'
  apiUrl?: string
  deviceToken?: string
}

type ApiModule = typeof import('../index')

/** 每次都用新模块实例：`api/index.ts` 在 import 时就快照了 `globalThis.chironDesktop` */
async function loadApi(host: Host): Promise<ApiModule> {
  vi.resetModules()
  ;(globalThis as { chironDesktop?: Host }).chironDesktop = host
  try {
    localStorage.clear()
  } catch {
    /* jsdom 存储不可用时不影响本用例 */
  }
  return await import('../index')
}

/** 让 adapter 固定回一个 503，并记录被调用次数 */
function alwaysUnavailable(
  mod: ApiModule,
  data: Record<string, unknown> = { detail: '本地 PostgreSQL 未启动（见 /readyz 的 pg 字段）' },
) {
  const calls: Array<Record<string, unknown>> = []
  mod.api.defaults.adapter = async (config) => {
    calls.push(config as unknown as Record<string, unknown>)
    return Promise.reject(
      Object.assign(new Error('Request failed with status code 503'), {
        isAxiosError: true,
        config,
        response: { status: 503, statusText: 'Service Unavailable', headers: {}, data, config },
      }),
    )
  }
  return calls
}

function captureToasts(): string[] {
  const seen: string[] = []
  window.addEventListener('api:error', (e) => {
    seen.push(String((e as CustomEvent<{ message?: string }>).detail?.message ?? ''))
  })
  return seen
}

/**
 * 按实现的递增节奏推进假时钟，把本地未就绪的重试全部放行（累计 20s）。
 *
 * 这里故意把间隔写死一遍而不是 import 常量：测试要钉住的是"用户实际等多久"
 * 这个对外承诺，实现改节奏时这里必须同步改 —— 否则测试会跟着实现一起漂移。
 */
async function drainLocalRetries(): Promise<void> {
  for (const ms of [1000, 2000, 3000, 4000, 5000, 5000]) {
    await vi.advanceTimersByTimeAsync(ms)
  }
}

beforeEach(() => {
  document.body.innerHTML = ''
})

afterEach(() => {
  delete (globalThis as { chironDesktop?: Host }).chironDesktop
  vi.restoreAllMocks()
  vi.useRealTimers()
})

describe('本地模式的 503（组件未就绪）', () => {
  it('走递增的本地节奏重试，不会像云端限流那样退避几十秒', async () => {
    vi.useFakeTimers()
    const mod = await loadApi({ isDesktop: true, mode: 'local', apiUrl: 'http://127.0.0.1:1' })
    const calls = alwaysUnavailable(mod)
    const toasts = captureToasts()

    const pending = mod.api.get('/v1/conversations').catch(() => undefined)

    // 递增间隔（1+2+3+4+5+5=20s）把首次启动窗口整段盖住，累计仍远小于云端单次退避
    await drainLocalRetries()
    await pending

    // 首次请求 + 6 次重试
    expect(calls.length).toBe(7)
    // 云端限流的"正在自动重试 X 秒后"提示**不应**出现
    expect(toasts.some(m => m.includes('正在自动重试') || m.includes('retrying automatically'))).toBe(false)
  })

  it('重试用尽后报本地未就绪，而不是"服务繁忙，请稍后再试"', async () => {
    vi.useFakeTimers()
    const mod = await loadApi({ isDesktop: true, mode: 'local', apiUrl: 'http://127.0.0.1:1' })
    alwaysUnavailable(mod)
    const toasts = captureToasts()

    const pending = mod.api.get('/v1/conversations').catch(() => undefined)
    await drainLocalRetries()
    await pending

    expect(toasts.length).toBeGreaterThan(0)
    const last = toasts[toasts.length - 1] ?? ''
    expect(last).toContain('本地服务还没准备好')
    expect(last).not.toContain('服务繁忙')
  })

  it('把后端 detail 原文带进提示（否则用户与运维都无从定位）', async () => {
    vi.useFakeTimers()
    const mod = await loadApi({ isDesktop: true, mode: 'local', apiUrl: 'http://127.0.0.1:1' })
    alwaysUnavailable(mod, { detail: '本地 PostgreSQL 未启动（见 /readyz 的 pg 字段）' })
    const toasts = captureToasts()

    const pending = mod.api.get('/v1/conversations').catch(() => undefined)
    await drainLocalRetries()
    await pending

    const last = toasts[toasts.length - 1] ?? ''
    expect(last).toContain('本地 PostgreSQL 未启动')
  })

  it('注入稳定 code，让界面语言切换后仍能取到本地化文案', async () => {
    vi.useFakeTimers()
    const mod = await loadApi({ isDesktop: true, mode: 'local', apiUrl: 'http://127.0.0.1:1' })
    alwaysUnavailable(mod)

    const pending = mod.api.get('/v1/conversations').catch(e => e)
    await drainLocalRetries()
    const error = await pending

    expect((error as { response?: { data?: { code?: string } } }).response?.data?.code).toBe(
      'local_service_not_ready',
    )
  })
})

describe('不该被本地分支影响的既有行为', () => {
  it('云端模式的 503 仍走限流的退避与提示（原行为不变）', async () => {
    vi.useFakeTimers()
    const mod = await loadApi({ isDesktop: true, mode: 'cloud' })
    alwaysUnavailable(mod, { error: 'rate limiter unavailable' })
    const toasts = captureToasts()

    const pending = mod.api.get('/v1/conversations').catch(() => undefined)
    await vi.advanceTimersByTimeAsync(60_000)
    await pending

    const joined = toasts.join('\n')
    expect(joined).toContain('服务繁忙')
    expect(joined).not.toContain('本地服务还没准备好')
  })

  it('本地模式下服务端显式 retryable:true 时，仍交给限流分支（服务端结论优先）', async () => {
    vi.useFakeTimers()
    const mod = await loadApi({ isDesktop: true, mode: 'local', apiUrl: 'http://127.0.0.1:1' })
    alwaysUnavailable(mod, { error: 'busy', retryable: true })
    const toasts = captureToasts()

    const pending = mod.api.get('/v1/conversations').catch(() => undefined)
    await vi.advanceTimersByTimeAsync(60_000)
    await pending

    const joined = toasts.join('\n')
    expect(joined).toContain('服务繁忙')
    expect(joined).not.toContain('本地服务还没准备好')
  })

  it('本地模式的 501 仍走"本地不支持该功能"分支', async () => {
    const mod = await loadApi({ isDesktop: true, mode: 'local', apiUrl: 'http://127.0.0.1:1' })
    mod.api.defaults.adapter = async (config) =>
      Promise.reject(
        Object.assign(new Error('Request failed with status code 501'), {
          isAxiosError: true,
          config,
          response: { status: 501, statusText: 'Not Implemented', headers: {}, data: {}, config },
        }),
      )
    const toasts = captureToasts()

    await mod.api.get('/v1/admin/backup').catch(() => undefined)

    const joined = toasts.join('\n')
    expect(joined).toContain('本地模式暂不支持该功能')
    expect(joined).not.toContain('服务繁忙')
  })
})
