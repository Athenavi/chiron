import { describe, it, expect, vi, beforeEach } from 'vitest'
import { mount, flushPromises } from '@vue/test-utils'
import { createRouter, createMemoryHistory } from 'vue-router'

/**
 * `SkillsView` 的冒烟网 + **`collectSkillUsage` 的接线**（2026-10-09）。
 *
 * 六视图补网第 4 个（`RegisterView` 487 → `KnowledgeView` 589 → `PluginsView` 702 → 本轮 854）。
 *
 * **为什么对用户重要**：`:452-456` 的"被哪些 Agent 使用"是**删技能前**要看的信息 ——
 * 接线断了不会崩，只会**静默不显示** ⇒ 用户会在不知情的情况下删掉别人在用的技能 ✓。
 */

if (!window.matchMedia) {
  window.matchMedia = ((query: string) => ({
    matches: false, media: query, onchange: null,
    addListener: () => {}, removeListener: () => {},
    addEventListener: () => {}, removeEventListener: () => {}, dispatchEvent: () => false,
  })) as unknown as typeof window.matchMedia
}

const SKILL = { name: 'summarize', description: '摘要', category: 'text' }

const apiMocks = vi.hoisted(() => ({
  // 显式 `Promise<unknown>`：否则推断成 `never[]`，`mockResolvedValue([...])` 会被 tsc 拒掉
  listAgents: vi.fn((): Promise<unknown> => Promise.resolve([])),
  listMarket: vi.fn((): Promise<unknown> => Promise.resolve([])),
  installMarket: vi.fn((): Promise<unknown> => Promise.resolve({})),
  // 形状按源码：`/v1/skills` → `response.data?.data?.skills`（`:65`）
  get: vi.fn((url?: string): Promise<unknown> => {
    if (url === '/v1/skills') return Promise.resolve({ data: { data: { skills: [] } } })
    return Promise.resolve({ data: { data: [] } })
  }),
  post: vi.fn((): Promise<unknown> => Promise.resolve({ data: {} })),
  put: vi.fn((): Promise<unknown> => Promise.resolve({ data: {} })),
  delete: vi.fn((): Promise<unknown> => Promise.resolve({ data: {} })),
}))

vi.mock('../../api', () => ({
  api: { get: apiMocks.get, post: apiMocks.post, put: apiMocks.put, delete: apiMocks.delete },
  listAgents: apiMocks.listAgents,
  listMarket: apiMocks.listMarket,
  installMarket: apiMocks.installMarket,
}))

import SkillsView from '../SkillsView.vue'

const router = createRouter({
  history: createMemoryHistory(),
  routes: [{ path: '/', component: { template: '<div />' } }],
})

function mountView() {
  return mount(SkillsView, { global: { plugins: [router] } })
}

const titlesOf = (wrapper: ReturnType<typeof mountView>) =>
  wrapper.findAll('[title]').map(e => e.attributes('title') || '')

describe('SkillsView（此前零测试）', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    apiMocks.get.mockImplementation((url?: string): Promise<unknown> => {
      if (url === '/v1/skills') return Promise.resolve({ data: { data: { skills: [SKILL] } } })
      return Promise.resolve({ data: { data: [] } })
    })
    apiMocks.listAgents.mockResolvedValue([])
    apiMocks.listMarket.mockResolvedValue([])
  })

  it('能挂载、跑完初始加载、卸载不抛错', async () => {
    const wrapper = mountView()
    await flushPromises()

    expect(apiMocks.get).toHaveBeenCalledWith('/v1/skills')
    expect(apiMocks.listAgents).toHaveBeenCalled()
    expect(() => wrapper.unmount()).not.toThrow()
  })

  it('★ 来源一：Agent 的 skills 列 ⇒ 悬浮提示里出现该 Agent', async () => {
    apiMocks.listAgents.mockResolvedValue([{ name: '归档助手', skills: ['summarize'] }])

    const wrapper = mountView()
    await flushPromises()

    expect(wrapper.text()).toContain('summarize')
    expect(titlesOf(wrapper).some(t => t.includes('归档助手'))).toBe(true)
    wrapper.unmount()
  })

  it('★ 没有 Agent 引用时不显示使用方标记（避免噪音）', async () => {
    const wrapper = mountView()
    await flushPromises()

    expect(wrapper.text()).toContain('summarize')
    expect(titlesOf(wrapper).some(t => t.includes('归档助手') || t.includes('日报流程'))).toBe(false)
    wrapper.unmount()
  })
})
