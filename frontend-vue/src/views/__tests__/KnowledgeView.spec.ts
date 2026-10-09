import { describe, it, expect, vi, beforeEach } from 'vitest'
import { mount, flushPromises } from '@vue/test-utils'
import { createRouter, createMemoryHistory } from 'vue-router'

/**
 * `KnowledgeView` 的冒烟网 + **`collectKbUsage` 的接线**（2026-10-09）。
 *
 * 第 133 轮扫出"六个视图零测试"后，本轮按**从小到大**的顺序补第二个
 * （`KnowledgeView` 589 行，上一轮补的是 `RegisterView` 487 行）。
 *
 * **为什么补这条接线断言**：`collectKbUsage` 自己**有单测** ✓（`utils/__tests__/kbUsage.spec.ts`），
 * 但"视图到底有没有把 `listAgents()` 的结果喂给它、并把它显示成「被哪些 Agent 使用」"**没人测** ✗。
 * 这条对用户是**可操作信息**：删知识库前要看清**谁在用它**（`KnowledgeView.vue:303-307`）——
 * 接线断了不会崩，只会**静默地不显示**，用户就会在不知情的情况下删掉别人在用的库 ✓。
 */

if (!window.matchMedia) {
  window.matchMedia = ((query: string) => ({
    matches: false, media: query, onchange: null,
    addListener: () => {}, removeListener: () => {},
    addEventListener: () => {}, removeEventListener: () => {}, dispatchEvent: () => false,
  })) as unknown as typeof window.matchMedia
}

const KB = {
  id: 'kb-1', name: '产品手册', description: '', type: 'wiki', visibility: 'private',
  status: 'ready', document_count: 3, total_size_bytes: 1024, credits_consumed: 0,
  created_at: '2026-10-01T00:00:00Z', updated_at: '2026-10-01T00:00:00Z',
}

const apiMocks = vi.hoisted(() => ({
  // 显式标成 `Promise<unknown>`：`vi.fn(() => Promise.resolve([]))` 会把返回类型推断成 `never[]`，
  // 后面 `mockResolvedValue([KB])` 就会被 tsc 拒掉（第一版就是这么红的）。
  listAgents: vi.fn((): Promise<unknown> => Promise.resolve([])),
  setKBVisibility: vi.fn((): Promise<unknown> => Promise.resolve({})),
  // ⚠ 形状是 `res.data.data.knowledge_bases`（`KnowledgeView.vue:96`）——
  // 给 `res.data.data` 是数组会得到空列表（第一版就栽在这，页面显示"暂无知识库"）。
  get: vi.fn((): Promise<unknown> => Promise.resolve({ data: { data: { knowledge_bases: [] } } })),
  post: vi.fn((): Promise<unknown> => Promise.resolve({ data: {} })),
  put: vi.fn((): Promise<unknown> => Promise.resolve({ data: {} })),
  delete: vi.fn((): Promise<unknown> => Promise.resolve({ data: {} })),
}))

vi.mock('../../api', () => ({
  api: { get: apiMocks.get, post: apiMocks.post, put: apiMocks.put, delete: apiMocks.delete },
  listAgents: apiMocks.listAgents,
  setKBVisibility: apiMocks.setKBVisibility,
}))

import KnowledgeView from '../KnowledgeView.vue'

const router = createRouter({
  history: createMemoryHistory(),
  routes: [{ path: '/', component: { template: '<div />' } }],
})

function mountView() {
  return mount(KnowledgeView, { global: { plugins: [router] } })
}

describe('KnowledgeView（此前零测试）', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    apiMocks.get.mockResolvedValue({ data: { data: { knowledge_bases: [KB] } } })
    apiMocks.listAgents.mockResolvedValue([])
  })

  it('能挂载、跑完初始加载、卸载不抛错', async () => {
    const wrapper = mountView()
    await flushPromises()

    expect(apiMocks.get).toHaveBeenCalledWith('/v1/kb')
    expect(apiMocks.listAgents).toHaveBeenCalled()      // 使用情况也在初始加载里取
    expect(() => wrapper.unmount()).not.toThrow()
  })

  it('★ 有 Agent 引用该知识库时显示使用摘要 + 悬浮提示里能查到是谁（collectKbUsage 的接线）', async () => {
    // `kb_id` 是 `collectKbUsage` 认的字段（`utils/kbUsage.ts:61`）
    apiMocks.listAgents.mockResolvedValue([{ name: '归档助手', kb_id: 'kb-1' }])

    const wrapper = mountView()
    await flushPromises()

    const text = wrapper.text()
    expect(text).toContain('产品手册')

    // 可见的是**数量摘要**（`usageLabel` → `t('agent.n_agents', {n})`，`:64-67`），
    // 具体是谁在**悬浮提示**里（`usageTitle`，`:70-73`）—— 两处都要接上才算"用户查得到谁在用"。
    expect(text).toContain('1')            // 摘要里有数量
    const titled = wrapper.findAll('[title]').map(e => e.attributes('title') || '')
    expect(titled.some(t => t.includes('归档助手'))).toBe(true)
    wrapper.unmount()
  })

  it('★ 没有 Agent 引用时不显示使用方标签（避免噪音）', async () => {
    const wrapper = mountView()
    await flushPromises()

    expect(wrapper.text()).toContain('产品手册')
    const titled = wrapper.findAll('[title]').map(e => e.attributes('title') || '')
    expect(titled.some(t => t.includes('归档助手'))).toBe(false)
    wrapper.unmount()
  })
})
