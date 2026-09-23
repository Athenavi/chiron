import { afterEach, beforeAll, beforeEach, describe, expect, it, vi } from 'vitest'
import { enableAutoUnmount, flushPromises, mount } from '@vue/test-utils'
import ChatSidePanel from '../ChatSidePanel.vue'

const api = vi.hoisted(() => ({
  api: { get: vi.fn() },
  listTools: vi.fn(),
  quickExecute: vi.fn(),
}))
vi.mock('../../../api', () => api)

vi.mock('vue-router', () => ({ useRouter: () => ({ push: vi.fn() }) }))

// 侧栏底部新增的用户入口引入 authStore；本测试不注入 pinia，
// 故沿用既有 mock 风格把它 mock 掉。
vi.mock('../../../stores/auth', () => ({
  useAuthStore: () => ({ user: null, logout: vi.fn() }),
}))

enableAutoUnmount(afterEach)

beforeAll(() => {
  // 组件顶层就读 matchMedia 判定抽屉模式，jsdom 默认没有实现
  Object.defineProperty(window, 'matchMedia', {
    writable: true,
    value: vi.fn().mockImplementation((query: string) => ({
      matches: false,
      media: query,
      onchange: null,
      addEventListener: vi.fn(),
      removeEventListener: vi.fn(),
      // ant-design-vue 的 responsiveObserve 仍走旧 API（会话视图会触发），
      // 缺这两个方法会抛 `mql.addListener is not a function`
      addListener: vi.fn(),
      removeListener: vi.fn(),
      dispatchEvent: vi.fn(),
    })),
  })
})

beforeEach(() => {
  vi.clearAllMocks()
})

/** open: false —— 活动轮询会直接返回，测试只关心「可用工具」区块 */
function mountPanel(
  sessions: any[] = [],
  view: 'trajectory' | 'sessions' | 'agents' | 'stats' = 'trajectory',
) {
  return mount(ChatSidePanel, {
    props: {
      items: [],
      selectedIndex: null,
      open: false,
      view,
      sessions,
      activeSessionId: '',
      userName: '测试员',
      contextChips: [],
    },
    // 底部主题 / 用户入口的两个子组件各自依赖 pinia（themeStore / authStore）。
    // 本测试只关心「可用工具」区块，故 stub 掉它们 —— 保持测试不引入 pinia，
    // 与上面 mock api / vue-router 的风格一致。
    global: {
      stubs: { ThemeSwitcher: true, SettingsPanel: true },
    },
  })
}

async function expand(w: ReturnType<typeof mountPanel>) {
  await w.find('.tools-head').trigger('click')
  await flushPromises()
}

describe('ChatSidePanel · 分支会话标记（P0：让"分支"在会话列表里可辨）', () => {
  const branchSession = {
    id: 'b1',
    title: '分支会话',
    created_at: '2026-09-23T10:00:00Z',
    updated_at: '2026-09-23T10:00:00Z',
    parent_session_id: '11111111-1111-1111-1111-111111111111',
    parent_title: '父会话',
    branch_from_seq: 12,
  }
  const plainSession = {
    id: 's1',
    title: '普通会话',
    created_at: '2026-09-23T11:00:00Z',
    updated_at: '2026-09-23T11:00:00Z',
  }

  it('分支会话显示「分支」徽标，悬浮说明给出父会话与分叉点', async () => {
    const w = mountPanel([branchSession, plainSession], 'sessions')
    await flushPromises()

    const badges = w.findAll('.session-branch')
    expect(badges).toHaveLength(1)
    expect(badges[0].text()).toBe('分支')
    expect(badges[0].attributes('title')).toContain('分支自《父会话》')
    expect(badges[0].attributes('title')).toContain('第 12 条起')
  })

  it('父会话已删（后端查不到展示名）时不编造来源，只说明分叉点', async () => {
    const w = mountPanel([{ ...branchSession, parent_title: '' }], 'sessions')
    await flushPromises()

    const tip = w.find('.session-branch').attributes('title') || ''
    expect(tip).toContain('已删除')
    expect(tip).toContain('第 12 条起')
  })

  it('普通会话没有分支徽标（否则所有会话都被误判为分支）', async () => {
    const w = mountPanel([plainSession], 'sessions')
    await flushPromises()

    expect(w.find('.session-branch').exists()).toBe(false)
  })
})

describe('ChatSidePanel · 可用工具（让 MCP 注入的工具可见）', () => {
  it('默认收起，且不请求工具列表（面板常驻挂载，不能白打接口）', () => {
    const w = mountPanel()

    expect(w.text()).toContain('可用工具')
    expect(w.find('.tools-body').exists()).toBe(false)
    expect(api.listTools).not.toHaveBeenCalled()
  })

  it('展开时才拉取，并列出工具名', async () => {
    api.listTools.mockResolvedValue([
      { name: 'read_file', description: '读文件', source: 'builtin' },
      { name: 'fs_read', description: '来自插件', source: 'mcp' },
    ])
    const w = mountPanel()
    await expand(w)

    expect(api.listTools).toHaveBeenCalledTimes(1)
    expect(w.text()).toContain('read_file')
    expect(w.text()).toContain('fs_read')
  })

  it('摘要报总数并单独报 MCP 数（这正是本区块存在的理由）', async () => {
    api.listTools.mockResolvedValue([
      { name: 'a', description: '', source: 'builtin' },
      { name: 'b', description: '', source: 'mcp' },
      { name: 'c', description: '', source: 'mcp' },
    ])
    const w = mountPanel()
    await expand(w)

    // 摘要现在直接列出 MCP 工具名 —— 只报数字说不清"到底激活了哪些能力"
    expect(w.find('.tools-count').text()).toBe('3 个 · MCP b, c')
  })

  it('只有内置工具时不提 MCP', async () => {
    api.listTools.mockResolvedValue([{ name: 'a', description: '', source: 'builtin' }])
    const w = mountPanel()
    await expand(w)

    expect(w.find('.tools-count').text()).toBe('1 个')
  })

  it('MCP 工具带徽标；内置与缺失 source 的都不带', async () => {
    api.listTools.mockResolvedValue([
      { name: 'plain', description: '', source: 'builtin' },
      { name: 'from_plugin', description: '', source: 'mcp' },
      { name: 'no_source', description: '' },
    ])
    const w = mountPanel()
    await expand(w)

    const rows = w.findAll('.tool-row')
    expect(rows).toHaveLength(3)
    expect(rows.filter(r => r.find('.tool-badge').exists()).map(r => r.find('.tool-name').text()))
      .toEqual(['from_plugin'])
  })

  it('工具描述挂在 title 上（列表只放名字，靠悬浮看细节）', async () => {
    api.listTools.mockResolvedValue([{ name: 'read_file', description: '读一个文件', source: 'builtin' }])
    const w = mountPanel()
    await expand(w)

    expect(w.find('.tool-row').attributes('title')).toBe('读一个文件')
  })

  it('加载失败时给出失败提示，而不是空白区块', async () => {
    api.listTools.mockRejectedValue(new Error('engine down'))
    const w = mountPanel()
    await expand(w)

    expect(w.text()).toContain('工具列表加载失败')
    expect(w.find('.tools-count').text()).toBe('加载失败')
  })

  it('没有可用工具时给出空态', async () => {
    api.listTools.mockResolvedValue([])
    const w = mountPanel()
    await expand(w)

    expect(w.text()).toContain('没有可用工具')
    expect(w.find('.tools-count').text()).toBe('无')
  })

  it('收起再展开不重复请求（成功结果缓存）', async () => {
    api.listTools.mockResolvedValue([{ name: 'x', description: '', source: 'builtin' }])
    const w = mountPanel()
    await expand(w)
    await w.find('.tools-head').trigger('click') // 收起
    await expand(w) // 再展开

    expect(api.listTools).toHaveBeenCalledTimes(1)
  })

  it('失败后再次展开会重试（不把失败也缓存住）', async () => {
    api.listTools.mockRejectedValue(new Error('engine down'))
    const w = mountPanel()
    await expand(w)

    api.listTools.mockResolvedValue([{ name: 'x', description: '', source: 'mcp' }])
    await w.find('.tools-head').trigger('click') // 收起
    await expand(w)

    expect(api.listTools).toHaveBeenCalledTimes(2)
    expect(w.text()).toContain('x')
    expect(w.find('.tools-count').text()).toBe('1 个 · MCP x')
  })
})
