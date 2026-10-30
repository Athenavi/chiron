import { describe, it, expect, vi, beforeEach } from 'vitest'
import { mount, flushPromises } from '@vue/test-utils'

/**
 * 设置面板的冒烟网（抽 composable 前置）。
 *
 * 为什么先补这几条：`SettingsPanel.vue` 是 1236 行且零测试的组件，页面与头像弹窗
 * 两个入口共用它。在这种体量上直接抽 composable 等于盲改 —— 搬错一个 `ref` 的归属
 * 只会静默显示旧值，不报错。这里钉住的是前两个子域里最容易搬坏的东西：
 * 「账户资料」的初始值与提交载荷、「三方绑定」的三态互斥与已绑定过滤。
 *
 * 选择器一律用组件/文案，不为测试往模板里加 `data-test` 钩子。
 */

const apiMocks = vi.hoisted(() => ({
  get: vi.fn(),
  put: vi.fn(),
  post: vi.fn(),
  delete: vi.fn(),
}))

const authApiMocks = vi.hoisted(() => ({
  listIdentities: vi.fn(),
  deleteIdentity: vi.fn(),
  listPublicSsoProviders: vi.fn(),
  ssoLoginURL: vi.fn((id: string, mode: string) => `https://sso.test/${id}?mode=${mode}`),
  getSmsBind: vi.fn(),
  bindPhone: vi.fn(),
  unbindPhone: vi.fn(),
  sendSmsCode: vi.fn(),
  isValidPhone: vi.fn(() => true),
}))

const authStoreMock = vi.hoisted(() => ({
  user: { name: 'Alice', email: 'alice@example.com', settings: {} as Record<string, unknown> },
  fetchProfile: vi.fn(async () => {}),
}))

const deviceStoreMock = vi.hoisted(() => ({
  isLocal: () => false,
  isDesktop: false,
}))

const themeStoreMock = vi.hoisted(() => ({
  preference: 'light',
  accent: '#000000',
  toggleTheme: vi.fn(),
  setAccent: vi.fn(),
}))

// antd 的 Tabs / Slider 走 responsiveObserve，jsdom 没有 matchMedia。
if (!window.matchMedia) {
  window.matchMedia = ((query: string) => ({
    matches: false,
    media: query,
    onchange: null,
    addListener: () => {},
    removeListener: () => {},
    addEventListener: () => {},
    removeEventListener: () => {},
    dispatchEvent: () => false,
  })) as unknown as typeof window.matchMedia
}

vi.mock('../../../api', () => ({ api: apiMocks }))
vi.mock('../../../api/auth', () => authApiMocks)
vi.mock('../../../stores/auth', () => ({ useAuthStore: () => authStoreMock }))
vi.mock('../../../stores/theme', () => ({ useThemeStore: () => themeStoreMock }))
vi.mock('../../../stores/device', () => ({ useDeviceStore: () => deviceStoreMock }))
vi.mock('vue-router', () => ({ useRoute: () => ({ query: {} }) }))

/**
 * `tallyCache` / `scheduleTally` 是「延后 + TTL 缓存」的组合器，这里压平成直通：
 * 测试要的是「取到值后视图变成什么样」，不是延后本身的时序。
 */
vi.mock('../../../utils/tallyCache', () => ({
  tallyCache: {
    load: vi.fn(async (_key: string, fetcher: () => Promise<unknown>) => fetcher()),
  },
  scheduleTally: vi.fn(
    async (
      _cache: unknown,
      _key: string,
      fetcher: () => Promise<unknown>,
      onValue: (v: unknown) => void,
      hooks: { onStart?: () => void; onError?: (e: unknown) => void },
    ) => {
      hooks.onStart?.()
      try {
        onValue(await fetcher())
      } catch (e) {
        hooks.onError?.(e)
      }
      return () => {}
    },
  ),
}))

import SettingsPanel from '../SettingsPanel.vue'
import EmptyState from '../../common/EmptyState.vue'
import { i18n } from '../../../i18n'

const tr = (key: string, args?: Record<string, unknown>): string =>
  (i18n.global as unknown as { t: (k: string, a?: Record<string, unknown>) => string }).t(key, args)

const STUBS = {
  HarnessProfilePanel: true,
  RunModePanel: true,
  LocalModelPanel: true,
  LanguageSwitcher: true,
}

function mountAccount() {
  return mount(SettingsPanel, {
    props: { initialTab: 'account' },
    global: { plugins: [i18n], stubs: STUBS },
  })
}

function buttonByText(wrapper: ReturnType<typeof mountAccount>, text: string) {
  const hit = wrapper.findAll('button').find(b => b.text().includes(text))
  if (!hit) throw new Error(`找不到按钮：${text}`)
  return hit
}

/**
 * 「基本资料」区块。
 *
 * 必须限定作用域：antd Tabs 在测试环境未被解析，所有 pane 的内容都同时存在于 DOM 里，
 * 直接 `findAll('input')[0]` 拿到的是「通用」页签的强调色输入框，不是姓名框。
 */
function profileBlock(wrapper: ReturnType<typeof mountAccount>) {
  const hit = wrapper
    .findAll('.setting-block')
    .find(b => b.text().includes(tr('settings.basic_profile')))
  if (!hit) throw new Error('找不到「基本资料」区块')
  return hit
}

/**
 * 「三方账号绑定」区块。同理必须限定：`EmptyState` 全文出现 5 次、`Popconfirm` 4 次，
 * 不限定作用域时 `findComponent` 取的是 DOM 里第一个，可能落在「通用」页签上。
 */
function bindingsBlock(wrapper: ReturnType<typeof mountAccount>) {
  const hit = wrapper
    .findAll('.setting-block')
    .find(b => b.text().includes(tr('auth.third_party_account_binding')))
  if (!hit) throw new Error('找不到「三方账号绑定」区块')
  return hit
}

describe('SettingsPanel 账户资料', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    apiMocks.put.mockResolvedValue({ data: { data: {} } })
  })

  it('挂载后姓名与邮箱取自当前登录用户', async () => {
    const wrapper = mountAccount()
    await flushPromises()

    const inputs = profileBlock(wrapper).findAll('input')
    expect((inputs[0].element as HTMLInputElement).value).toBe('Alice')
    expect((inputs[1].element as HTMLInputElement).value).toBe('alice@example.com')
  })

  it('邮箱是只读的（改邮箱要走验证流程，不在此处）', async () => {
    const wrapper = mountAccount()
    await flushPromises()

    expect((profileBlock(wrapper).findAll('input')[1].element as HTMLInputElement).disabled).toBe(true)
  })

  it('点保存把当前表单值原样提交，成功后刷新用户资料', async () => {
    const wrapper = mountAccount()
    await flushPromises()
    authStoreMock.fetchProfile.mockClear()

    await profileBlock(wrapper).findAll('input')[0].setValue('Alice Chen')
    await buttonByText(wrapper, tr('common.save_changes')).trigger('click')
    await flushPromises()

    expect(apiMocks.put).toHaveBeenCalledWith('/v1/auth/profile', {
      name: 'Alice Chen',
      email: 'alice@example.com',
    })
    expect(authStoreMock.fetchProfile).toHaveBeenCalled()
  })

  it('提交失败不刷新资料（避免用旧值覆盖界面上已改的内容）', async () => {
    apiMocks.put.mockRejectedValueOnce(new Error('boom'))
    const wrapper = mountAccount()
    await flushPromises()
    authStoreMock.fetchProfile.mockClear()

    await buttonByText(wrapper, tr('common.save_changes')).trigger('click')
    await flushPromises()

    expect(authStoreMock.fetchProfile).not.toHaveBeenCalled()
  })
})

describe('SettingsPanel 三方账号绑定', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    authApiMocks.listIdentities.mockResolvedValue([])
    authApiMocks.listPublicSsoProviders.mockResolvedValue([])
  })

  it('既没绑定也没可绑定时显示空态', async () => {
    const wrapper = mountAccount()
    await flushPromises()

    expect(bindingsBlock(wrapper).findComponent(EmptyState).exists()).toBe(true)
  })

  it('已绑定的 provider 不再出现在可绑定列表里', async () => {
    authApiMocks.listIdentities.mockResolvedValue([
      { id: 'i1', provider_name: 'GitHub', provider_type: 'oidc', email: 'a@x.com', subject: 's' },
    ])
    authApiMocks.listPublicSsoProviders.mockResolvedValue([
      { id: 'p1', name: 'github', display_name: 'GitHub' },
      { id: 'p2', name: 'gitlab', display_name: 'GitLab' },
    ])

    const wrapper = mountAccount()
    await flushPromises()

    const rows = wrapper.findAll('.identity-row')
    expect(rows).toHaveLength(1)
    expect(rows[0].text()).toContain('GitHub')

    // 只剩 GitLab 可绑：GitHub 已被过滤掉，避免点了重复绑定
    const bindButtons = wrapper.findAll('.bind-buttons button')
    expect(bindButtons).toHaveLength(1)
    expect(bindButtons[0].text()).toContain('GitLab')
  })

  it('解绑后重新拉取列表（不能只删本地一行，后端可能还有别的变化）', async () => {
    authApiMocks.listIdentities.mockResolvedValue([
      { id: 'i1', provider_name: 'GitHub', provider_type: 'oidc', email: 'a@x.com', subject: 's' },
    ])
    authApiMocks.deleteIdentity.mockResolvedValue(undefined)

    const wrapper = mountAccount()
    await flushPromises()
    const callsBefore = authApiMocks.listIdentities.mock.calls.length

    // Popconfirm 的确认在 jsdom 里不弹层，直接走它绑定的事件。
    // 必须限定在绑定区块内：全文有 4 个 Popconfirm，全局取第一个会解绑错东西。
    const popconfirm = bindingsBlock(wrapper).findComponent({ name: 'APopconfirm' })
    expect(popconfirm.exists()).toBe(true)
    await popconfirm.vm.$emit('confirm')
    await flushPromises()

    expect(authApiMocks.deleteIdentity).toHaveBeenCalledWith('i1')
    expect(authApiMocks.listIdentities.mock.calls.length).toBeGreaterThan(callsBefore)
  })
})
