import { afterEach, describe, expect, it, vi } from 'vitest'

/**
 * 本地模式的 Bearer 头（契约 §4）。
 *
 * 桌面端本地 core 可以打开设备 token 鉴权（`CHIRON_LOCAL_AUTH_ENABLED=1`），此时它只认
 * preload 注入的 `window.chironDesktop.deviceToken`。这里钉住三件事：
 *
 * 1. 本地模式带的是**设备 token**；
 * 2. 没有设备 token 就**不设** Authorization（默认鉴权关闭，无 token 本就合法）；
 * 3. 本地 token **不进** safeStorage —— 否则切回云端模式会被当成登录 token 发出去。
 */

type Host = {
  isDesktop?: boolean
  mode?: 'local' | 'cloud'
  deviceToken?: string
  apiUrl?: string
  saveToken?: (token: string) => Promise<unknown>
  clearToken?: () => Promise<unknown>
}

type ApiModule = typeof import('../index')

/**
 * 每次都用**新的模块实例**：`api/index.ts` 在 import 时就快照了
 * `globalThis.chironDesktop`，同一模块里改不到。
 */
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

/** 用自定义 adapter 抓请求头，不真发网络 */
function captureHeaders(mod: ApiModule): Array<Record<string, unknown>> {
  const seen: Array<Record<string, unknown>> = []
  mod.api.defaults.adapter = async (config) => {
    seen.push(config.headers as unknown as Record<string, unknown>)
    return { data: { ok: true }, status: 200, statusText: 'OK', headers: {}, config }
  }
  return seen
}

function authorizationOf(headers: Record<string, unknown>): unknown {
  const getter = (headers as { get?: (name: string) => unknown }).get
  return typeof getter === 'function' ? getter.call(headers, 'Authorization') : headers.Authorization
}

afterEach(() => {
  delete (globalThis as { chironDesktop?: Host }).chironDesktop
  vi.restoreAllMocks()
})

describe('本地模式的 Authorization 头', () => {
  it('local 模式带 preload 注入的设备 token', async () => {
    const mod = await loadApi({ isDesktop: true, mode: 'local', deviceToken: 'dev-token-1' })
    const seen = captureHeaders(mod)
    await mod.api.get('/v1/models')
    expect(authorizationOf(seen[0]!)).toBe('Bearer dev-token-1')
  })

  it('设备 token 两侧空白被裁掉（argv 注入偶尔带空格）', async () => {
    const mod = await loadApi({ isDesktop: true, mode: 'local', deviceToken: '  dev-token-2  ' })
    const seen = captureHeaders(mod)
    await mod.api.get('/v1/models')
    expect(authorizationOf(seen[0]!)).toBe('Bearer dev-token-2')
  })

  it('local 模式没有设备 token 时不设 Authorization', async () => {
    const mod = await loadApi({ isDesktop: true, mode: 'local', deviceToken: '' })
    const seen = captureHeaders(mod)
    await mod.api.get('/v1/models')
    expect(authorizationOf(seen[0]!)).toBeUndefined()
  })

  it('local 模式不会把云端 token 当作 Bearer 发出去', async () => {
    const mod = await loadApi({ isDesktop: true, mode: 'local', deviceToken: '' })
    mod.setAuthToken('cloud-token-from-login')
    const seen = captureHeaders(mod)
    await mod.api.get('/v1/models')
    expect(authorizationOf(seen[0]!)).toBeUndefined()
  })

  it('cloud 模式仍用登录 token（设备 token 不参与）', async () => {
    const mod = await loadApi({ isDesktop: true, mode: 'cloud', deviceToken: 'dev-token-3' })
    mod.setAuthToken('cloud-token')
    const seen = captureHeaders(mod)
    await mod.api.get('/v1/models')
    expect(authorizationOf(seen[0]!)).toBe('Bearer cloud-token')
  })

  it('Web 端（无桌面外壳）不设 Authorization，靠 httpOnly cookie', async () => {
    const mod = await loadApi({})
    const seen = captureHeaders(mod)
    await mod.api.get('/v1/models')
    expect(authorizationOf(seen[0]!)).toBeUndefined()
  })
})

describe('setAuthToken 的持久化边界', () => {
  it('local 模式下不把设备 token 写进 safeStorage', async () => {
    const saveToken = vi.fn(async () => undefined)
    const mod = await loadApi({ isDesktop: true, mode: 'local', deviceToken: 'dev-token-4', saveToken })
    mod.setAuthToken('dev-token-4')
    expect(saveToken).not.toHaveBeenCalled()
  })

  it('cloud 模式下照旧交给主进程加密保存', async () => {
    const saveToken = vi.fn(async () => undefined)
    const mod = await loadApi({ isDesktop: true, mode: 'cloud', saveToken })
    mod.setAuthToken('cloud-token')
    expect(saveToken).toHaveBeenCalledWith('cloud-token')
  })
})
