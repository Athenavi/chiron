import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { mount, flushPromises } from '@vue/test-utils'

vi.mock('../../api/auth', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../../api/auth')>()
  return {
    ...actual,
    listPublicSsoProviders: vi.fn(),
    // ssoLoginURL 包一层 mock：组件里是 `window.location.href = ssoLoginURL(...)`，
    // 而 jsdom 的 location 在部分 pool（如 vmThreads）下**不可重写** ——
    // 原先靠 Object.defineProperty 替换 window.location 会直接 TypeError
    // （且那段 fallback 写在抛错语句之后，永远执行不到）。
    // 改为拦截 URL 构造入口，既能验证"点击真的走到了跳转逻辑"，
    // 又不依赖 jsdom 内部实现；返回值仍是真实实现，纯函数语义不变。
    ssoLoginURL: vi.fn(actual.ssoLoginURL),
  }
})

import SsoLoginButtons from '../SsoLoginButtons.vue'
import { listPublicSsoProviders, ssoLoginURL, type SsoPublicProvider } from '../../api/auth'

function makeProvider(overrides: Partial<SsoPublicProvider> = {}): SsoPublicProvider {
  return {
    id: 'prov-1',
    name: 'github-main',
    display_name: '',
    provider_type: 'github',
    icon: '',
    sort_order: 100,
    protocol: 'oauth2',
    ...overrides,
  }
}

describe('SsoLoginButtons', () => {
  let originalHref: string

  beforeEach(() => {
    vi.clearAllMocks()
    originalHref = window.location.href
  })

  afterEach(() => {
    window.location.href = originalHref
  })

  it('有启用的 provider 时渲染品牌按钮（display_name 缺省回落品牌名）', async () => {
    vi.mocked(listPublicSsoProviders).mockResolvedValue([
      makeProvider(),
      makeProvider({ id: 'prov-2', provider_type: 'wechat', display_name: '企业微信' }),
      makeProvider({ id: 'prov-3', provider_type: 'unknown-brand' }),
    ])
    const wrapper = mount(SsoLoginButtons)
    await flushPromises()

    const buttons = wrapper.findAll('.sso-btn')
    expect(buttons).toHaveLength(3)
    // github 无 display_name → 品牌名 GitHub
    expect(buttons[0].text()).toContain('GitHub')
    // wechat 有 display_name → 优先展示
    expect(buttons[1].text()).toContain('企业微信')
    // 未知 provider_type → 回落 custom 样式 SSO
    expect(buttons[2].text()).toContain('SSO')
  })

  it('无 provider 时整组隐藏（登录页主流程不受影响）', async () => {
    vi.mocked(listPublicSsoProviders).mockResolvedValue([])
    const wrapper = mount(SsoLoginButtons)
    await flushPromises()
    expect(wrapper.find('.sso-group').exists()).toBe(false)
  })

  it('接口失败时静默隐藏不抛错', async () => {
    vi.mocked(listPublicSsoProviders).mockRejectedValue(new Error('gateway down'))
    const wrapper = mount(SsoLoginButtons)
    await flushPromises()
    expect(wrapper.find('.sso-group').exists()).toBe(false)
  })

  it('login 模式分隔文案为「三方登录」', async () => {
    vi.mocked(listPublicSsoProviders).mockResolvedValue([makeProvider()])
    const wrapper = mount(SsoLoginButtons, { props: { mode: 'login' } })
    await flushPromises()
    expect(wrapper.find('.sso-divider').text()).toContain('三方登录')
  })

  it('bind 模式分隔文案为「绑定三方账号」', async () => {
    vi.mocked(listPublicSsoProviders).mockResolvedValue([makeProvider()])
    const wrapper = mount(SsoLoginButtons, { props: { mode: 'bind' } })
    await flushPromises()
    expect(wrapper.find('.sso-divider').text()).toContain('绑定三方账号')
  })

  it('点击 provider 整页跳转 SSO 登录地址', async () => {
    vi.mocked(listPublicSsoProviders).mockResolvedValue([makeProvider({ id: 'gh-1' })])
    vi.mocked(ssoLoginURL).mockClear()

    const wrapper = mount(SsoLoginButtons)
    await flushPromises()
    await wrapper.find('.sso-btn').trigger('click')

    // 断言"点击真的走到了跳转逻辑"：组件用 `window.location.href = ssoLoginURL(id)`，
    // 两者之间只有这一处调用点，所以"被以 gh-1 调用过"等价于"跳转目标正确"。
    // 不再尝试替换 window.location —— 那在 vmThreads 下会直接 TypeError。
    expect(ssoLoginURL).toHaveBeenCalledWith('gh-1', undefined)
    expect(vi.mocked(ssoLoginURL).mock.results[0]!.value).toContain('/v1/auth/sso/login/gh-1')
  })

  it('bind 模式跳转地址带 ?mode=bind', async () => {
    vi.mocked(listPublicSsoProviders).mockResolvedValue([makeProvider({ id: 'gh-2' })])
    const wrapper = mount(SsoLoginButtons, { props: { mode: 'bind' } })
    await flushPromises()
    await wrapper.find('.sso-btn').trigger('click')
    expect(ssoLoginURL('gh-2', 'bind')).toContain('/v1/auth/sso/login/gh-2?mode=bind')
  })
})

describe('ssoLoginURL（纯函数）', () => {
  it('provider id 做 URL 编码（防路径注入）', () => {
    const u = ssoLoginURL('a/b?c=d')
    expect(u).not.toContain('/a/b')
    expect(u).toContain(encodeURIComponent('a/b?c=d'))
  })

  it('无 mode 不附加 query', () => {
    const u = ssoLoginURL('x')
    expect(u.endsWith('/v1/auth/sso/login/x')).toBe(true)
  })
})
