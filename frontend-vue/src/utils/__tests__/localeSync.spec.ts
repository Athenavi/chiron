import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { DEFAULT_LOCALE } from '../../i18n/languages'

// partial mock：只替换网络客户端，保留 api 模块其它导出（setAuthToken 等）。
vi.mock('../../api', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../../api')>()
  return {
    ...actual,
    api: {
      get: vi.fn(),
      post: vi.fn(),
      put: vi.fn(),
    },
  }
})

// 设备模式可控：本地模式没有账号体系，应当完全跳过回写。
vi.mock('../../stores/device', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../../stores/device')>()
  return {
    ...actual,
    currentDeviceMode: vi.fn(() => 'cloud' as const),
  }
})

import { api } from '../../api'
import { currentDeviceMode } from '../../stores/device'
import { currentLocale, setLocale } from '../../i18n'
import { applyServerLocale, installLocaleSync, persistLocale } from '../localeSync'

const LOCALE_STORAGE_KEY = 'chiron.locale'

/**
 * `utils/localeSync.ts` 的测试（T1）。
 *
 * 钉住三件事，它们都是本轮修掉的真实缺口：
 *  1. 切语言要**回写到服务端** `users.settings.locale` —— 否则服务端渲染的邮件
 *     永远用中文（语言此前只存 localStorage）；
 *  2. 回写失败必须**静默** —— 语言已在本地生效，没有用户可执行的动作；
 *  3. 登录后按服务端偏好对齐时，**本机显式选择优先**，否则用户刚在登录页选的
 *     语言会被服务端旧值立刻覆盖，表现为「选择没生效」。
 */

beforeEach(() => {
  vi.mocked(api.put).mockReset().mockResolvedValue({ data: { data: {} } })
  vi.mocked(currentDeviceMode).mockReturnValue('cloud')
  localStorage.clear()
  // 清掉可能由上一个用例注册的 hook，避免相互污染。
  installLocaleSync(() => false)
})

afterEach(() => {
  localStorage.clear()
  setLocale(DEFAULT_LOCALE, { persist: false })
})

describe('persistLocale（回写服务端）', () => {
  it('已登录且云端模式：PUT users.settings.locale，且只传 locale 不覆盖其它设置', async () => {
    installLocaleSync(() => true)
    await persistLocale('en-US')

    expect(vi.mocked(api.put)).toHaveBeenCalledWith('/v1/auth/profile', {
      settings: { locale: 'en-US' },
    })
  })

  it('未登录：不发请求（匿名用户没有 users 行可写）', async () => {
    installLocaleSync(() => false)
    await persistLocale('ar')

    expect(vi.mocked(api.put)).not.toHaveBeenCalled()
  })

  it('本地模式：不发请求（没有 /v1/auth/profile）', async () => {
    vi.mocked(currentDeviceMode).mockReturnValue('local')
    installLocaleSync(() => true)
    await persistLocale('en-US')

    expect(vi.mocked(api.put)).not.toHaveBeenCalled()
  })

  it('写入失败静默：不抛错、不阻塞调用方', async () => {
    vi.mocked(api.put).mockRejectedValue(new Error('network'))
    installLocaleSync(() => true)

    // 关键断言是「不 reject」—— 语言切换不能被网络失败打断。
    await expect(persistLocale('en-US')).resolves.toBeUndefined()
  })
})

describe('installLocaleSync（与 setLocale 联动）', () => {
  it('切语言会触发回写', async () => {
    installLocaleSync(() => true)
    setLocale('en-US')

    // hook 内部是不 await 的 fire-and-forget，等一个微任务轮次。
    await vi.waitFor(() => {
      expect(vi.mocked(api.put)).toHaveBeenCalledWith('/v1/auth/profile', {
        settings: { locale: 'en-US' },
      })
    })
  })

  it('persist:false 时不回写（这是「服务端 → 本地」方向，回写会互相触发）', async () => {
    installLocaleSync(() => true)
    setLocale('ar', { persist: false })

    // 给足机会也不应被调用。
    await new Promise((r) => setTimeout(r, 0))
    expect(vi.mocked(api.put)).not.toHaveBeenCalled()
    expect(currentLocale()).toBe('ar')
  })
})

describe('applyServerLocale（登录后对齐）', () => {
  it('本机没有显式选择时采用服务端语言', () => {
    localStorage.clear()
    applyServerLocale({ locale: 'en-US' })

    expect(currentLocale()).toBe('en-US')
  })

  it('本机已显式选择时**不覆盖**（否则用户刚选的语言会立刻被改回去）', () => {
    setLocale('ar', { persist: false })
    expect(localStorage.getItem(LOCALE_STORAGE_KEY)).toBe('ar')

    applyServerLocale({ locale: 'en-US' })

    expect(currentLocale()).toBe('ar')
  })

  it('服务端没有 locale / 值非法时保持不动', () => {
    localStorage.clear()
    setLocale(DEFAULT_LOCALE, { persist: false })

    applyServerLocale(undefined)
    applyServerLocale({})
    applyServerLocale({ locale: '' })
    applyServerLocale({ locale: 123 as unknown as string })

    expect(currentLocale()).toBe(DEFAULT_LOCALE)
  })
})
