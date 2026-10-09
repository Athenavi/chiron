import { describe, it, expect, vi, beforeEach } from 'vitest'
import { mount, flushPromises } from '@vue/test-utils'
import { createRouter, createMemoryHistory } from 'vue-router'

/**
 * `PluginsView` 的冒烟网 + **`collectBindingUsage` 的接线**（2026-10-09）。
 *
 * 六视图补网第 3 个（第 133 轮起：`RegisterView` 487 → `KnowledgeView` 589 → 本轮 `PluginsView` 702）。
 *
 * **与 KnowledgeView 同源**：两处都走 `utils/kbUsage` 的 `collectBindingUsage`
 * （`PluginsView.vue:51` 传 `'plugins'` 列，KnowledgeView 传 `kb_id`）—— 但**列名不同、形状不同**，
 * 所以接线各测一次 ✓。`collectBindingUsage` 本身有单测 ✓，缺的仍是"视图有没有喂给它、有没有显示" ✓。
 *
 * **为什么这条对用户重要**：`PluginsView.vue:404-408` 的"被哪些 Agent 使用"是**删插件前**要看的信息 ——
 * 接线断了不会崩，只会**静默不显示** ⇒ 用户会在不知情的情况下删掉别人在用的插件 ✓。
 */

if (!window.matchMedia) {
  window.matchMedia = ((query: string) => ({
    matches: false, media: query, onchange: null,
    addListener: () => {}, removeListener: () => {},
    addEventListener: () => {}, removeEventListener: () => {}, dispatchEvent: () => false,
  })) as unknown as typeof window.matchMedia
}

const PLUGIN = { name: 'fs', command: 'mcp-fs', status: 'enabled', description: '文件系统' }

const apiMocks = vi.hoisted(() => ({
  // 显式 `Promise<unknown>`：否则 `vi.fn(() => Promise.resolve([]))` 推断成 `never[]`，
  // 后面 `mockResolvedValue([...])` 会被 tsc 拒掉（第 134 轮踩过）。
  listAgents: vi.fn((): Promise<unknown> => Promise.resolve([])),
  listMarket: vi.fn((): Promise<unknown> => Promise.resolve([])),
  installMarket: vi.fn((): Promise<unknown> => Promise.resolve({})),
  get: vi.fn((): Promise<unknown> => Promise.resolve({ data: { data: [] } })),
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

import PluginsView from '../PluginsView.vue'

const router = createRouter({
  history: createMemoryHistory(),
  routes: [{ path: '/', component: { template: '<div />' } }],
})

function mountView() {
  return mount(PluginsView, { global: { plugins: [router] } })
}

describe('PluginsView（此前零测试）', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    // 形状是 `res.data?.data`（`PluginsView.vue:136`），直接就是数组
    apiMocks.get.mockResolvedValue({ data: { data: [PLUGIN] } })
    apiMocks.listAgents.mockResolvedValue([])
    apiMocks.listMarket.mockResolvedValue([])
  })

  it('能挂载、跑完初始加载、卸载不抛错', async () => {
    const wrapper = mountView()
    await flushPromises()

    expect(apiMocks.get).toHaveBeenCalledWith('/v1/plugins')
    expect(apiMocks.listAgents).toHaveBeenCalled()   // 使用情况也在初始加载里取
    expect(() => wrapper.unmount()).not.toThrow()
  })

  it('★ 有 Agent 装配该插件时显示使用摘要 + 悬浮提示里能查到是谁（collectBindingUsage 的接线）', async () => {
    // `plugins` 是 `collectBindingUsage` 认的列名（`PluginsView.vue:51`）
    apiMocks.listAgents.mockResolvedValue([{ name: '归档助手', plugins: ['fs'] }])

    const wrapper = mountView()
    await flushPromises()

    expect(wrapper.text()).toContain('fs')
    // 可见的是**数量摘要**（`usageLabel`，`:58-62`），名字在**悬浮提示**里（`usageTitle`，`:64-68`）
    const titled = wrapper.findAll('[title]').map(e => e.attributes('title') || '')
    expect(titled.some(t => t.includes('归档助手'))).toBe(true)
    wrapper.unmount()
  })

  it('★ 没有 Agent 装配时不显示使用方标签（避免噪音）', async () => {
    const wrapper = mountView()
    await flushPromises()

    expect(wrapper.text()).toContain('fs')
    const titled = wrapper.findAll('[title]').map(e => e.attributes('title') || '')
    expect(titled.some(t => t.includes('归档助手'))).toBe(false)
    wrapper.unmount()
  })
})
