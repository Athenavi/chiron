import { describe, it, expect, vi, beforeEach } from 'vitest'
import { mount, flushPromises } from '@vue/test-utils'
import { createRouter, createMemoryHistory } from 'vue-router'

// jsdom 未实现 matchMedia，ant-design-vue 的 Tabs/Grid 等组件（useBreakpoint）依赖它
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
  })) as any
}

vi.mock('../../api/memory', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../../api/memory')>()
  return {
    ...actual,
    listMemory: vi.fn(),
    upsertMemory: vi.fn(),
    updateMemory: vi.fn(),
    deleteMemory: vi.fn(),
    clearMemory: vi.fn(),
    searchMemory: vi.fn(),
    startOrganize: vi.fn(),
    getOrganizeStatus: vi.fn(),
    listConflicts: vi.fn(),
    listSummaries: vi.fn(),
  }
})

import MemoryView from '../MemoryView.vue'
import * as memoryApi from '../../api/memory'
import type { MemorySearchHit } from '../../api/memory'

function makeEntry(overrides: Partial<MemorySearchHit> = {}): MemorySearchHit {
  return {
    id: 'mem-1',
    slot: 'preference',
    slot_label: '偏好',
    key: 'editor',
    value: 'VSCode',
    confidence: 80,
    source: 'user_confirmed',
    source_label: '用户确认',
    has_embedding: true,
    access_count: 3,
    last_accessed_at: null,
    status: 'active',
    created_at: '2026-08-21T00:00:00Z',
    updated_at: '2026-08-21T00:00:00Z',
    similarity: 0.8,
    score: 0.7,
    ...overrides,
  }
}

const router = createRouter({
  history: createMemoryHistory(),
  routes: [
    { path: '/', component: { template: '<div />' } },
    { path: '/chat', component: { template: '<div />' } },
  ],
})

/** 组件用 useRouter() 跳到对话，因此 mount 时必须提供 router 插件 */
function mountView() {
  return mount(MemoryView, { global: { plugins: [router] } })
}

describe('项目选择器（C3）', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    localStorage.clear()
    vi.mocked(memoryApi.listMemory).mockResolvedValue({
      entries: [],
      counts: { identity: 0, preference: 0, decision: 0, fact: 0 },
      total: 0,
      slots: [],
      organize: { running: false, started_at: null, finished_at: null, result: null, error: null },
    })
    vi.mocked(memoryApi.listConflicts).mockResolvedValue({ conflicts: [], count: 0 })
    vi.mocked(memoryApi.listSummaries).mockResolvedValue({ summaries: [], count: 0 })
    vi.mocked(memoryApi.getOrganizeStatus).mockResolvedValue({
      running: false,
      started_at: null,
      finished_at: null,
      result: null,
      error: null,
    })
  })

  it('切换项目会重新加载记忆并把项目持久化', async () => {
    const wrapper = mountView()
    await flushPromises()
    const before = vi.mocked(memoryApi.listMemory).mock.calls.length

    const input = wrapper.find('[data-test="project-input"]')
    await input.setValue('proj-a')
    await input.trigger('change')
    await flushPromises()

    expect(vi.mocked(memoryApi.listMemory).mock.calls.length).toBeGreaterThan(before)
    expect(localStorage.getItem('chiron.memory.project')).toBe('proj-a')
  })
})

describe('MemoryView', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    vi.mocked(memoryApi.listMemory).mockResolvedValue({
      entries: [],
      counts: { identity: 0, preference: 0, decision: 0, fact: 0 },
      total: 0,
      slots: [
        { slot: 'identity', label: '身份' },
        { slot: 'preference', label: '偏好' },
        { slot: 'decision', label: '关键决策' },
        { slot: 'fact', label: '长期事实' },
      ],
      organize: { running: false, started_at: null, finished_at: null, result: null, error: null },
    })
    vi.mocked(memoryApi.listConflicts).mockResolvedValue({ conflicts: [], count: 0 })
    vi.mocked(memoryApi.listSummaries).mockResolvedValue({ summaries: [], count: 0 })
  })

  /** 点开某个顶部 tab（ant-design-vue 的 Tabs 用 .ant-tabs-tab 承载标题） */
  async function openTab(wrapper: ReturnType<typeof mount>, label: string) {
    const tab = wrapper.findAll('.ant-tabs-tab').find((t) => t.text().includes(label))
    expect(tab, `tab "${label}" not found`).toBeTruthy()
    await tab!.trigger('click')
    await flushPromises()
  }

  it('渲染标题、检索提示与记忆操作骨架', async () => {
    const wrapper = mountView()
    await flushPromises()
    const text = wrapper.text()
    expect(text).toContain('长期记忆')
    // L2 档案卡设计意图（跨会话留存 · 语义检索 · 自动整理）
    expect(text).toContain('语义检索')
    expect(text).toContain('自动整理')
    // 三个核心操作按钮
    for (const label of ['智能整理', '清空记忆', '新建记忆']) {
      expect(text).toContain(label)
    }
    // 语义检索输入框（用 placeholder 定位：顶部还有一个「项目」输入框，
    // `find('input')` 这种位置选择器不再可靠）
    const input = wrapper.find('input[placeholder*="语义检索"]')
    expect(input.exists()).toBe(true)
    expect((input.element as HTMLInputElement).placeholder).toContain('语义检索')
  })

  it('挂载即加载记忆列表并展示条目', async () => {
    vi.mocked(memoryApi.listMemory).mockResolvedValue({
      entries: [makeEntry(), makeEntry({ id: 'mem-2', slot: 'fact', slot_label: '长期事实', key: 'stack', value: 'Go + Python' })],
      counts: { identity: 0, preference: 1, decision: 0, fact: 1 },
      total: 2,
      slots: [],
      organize: { running: false, started_at: null, finished_at: null, result: null, error: null },
    })
    const wrapper = mountView()
    await flushPromises()
    expect(memoryApi.listMemory).toHaveBeenCalledOnce()
    expect(wrapper.text()).toContain('editor')
    expect(wrapper.text()).toContain('VSCode')
    expect(wrapper.text()).toContain('Go + Python')
  })

  it('点击「智能检索」调用 searchMemory 并展示结果', async () => {
    vi.mocked(memoryApi.searchMemory).mockResolvedValue({
      query: '编辑器',
      mode: 'semantic',
      count: 1,
      results: [makeEntry({ value: 'VSCode', similarity: 0.92, score: 0.88 })],
      summaries: [],
      summary_count: 0,
    })
    const wrapper = mountView()
    await flushPromises()

    await wrapper.find('input[placeholder*="语义检索"]').setValue('编辑器')
    // 找到「智能检索」按钮并点击
    const buttons = wrapper.findAll('button')
    const searchBtn = buttons.find((b) => b.text().includes('智能检索'))
    expect(searchBtn).toBeTruthy()
    await searchBtn!.trigger('click')
    await flushPromises()

    expect(memoryApi.searchMemory).toHaveBeenCalledWith('编辑器', { top_k: 10 })
    expect(wrapper.text()).toContain('语义模式')
    expect(wrapper.text()).toContain('返回列表')
  })

  it('点击「智能整理」触发 startOrganize', async () => {
    vi.mocked(memoryApi.startOrganize).mockResolvedValue({
      started: true,
      status: { running: true, started_at: 1, finished_at: null, result: null, error: null },
    })
    const wrapper = mountView()
    await flushPromises()
    const buttons = wrapper.findAll('button')
    const orgBtn = buttons.find((b) => b.text().includes('智能整理'))
    expect(orgBtn).toBeTruthy()
    await orgBtn!.trigger('click')
    await flushPromises()
    expect(memoryApi.startOrganize).toHaveBeenCalledOnce()
  })

  it('「待裁决」分区展示冲突卡片（旧值 / 新值可读）', async () => {
    vi.mocked(memoryApi.listConflicts).mockResolvedValue({
      conflicts: [
        {
          conflict_id: 'cfl-1',
          slot: 'fact',
          item_key: 'city',
          old_value: '上海',
          new_value: '北京',
          source: 'derived',
          created_at: Date.now() / 1000,
        },
      ],
      count: 1,
    })
    const wrapper = mountView()
    await flushPromises()
    await openTab(wrapper, '待裁决')

    const text = wrapper.text()
    expect(text).toContain('记忆冲突')
    expect(text).toContain('上海')
    expect(text).toContain('北京')
    // 三条裁决路径必须都在，否则冲突只能被忽略
    for (const label of ['保留当前值', '采用新值', '手动修改']) {
      expect(text).toContain(label)
    }
  })

  it('「摘要」分区展示历史对话摘要', async () => {
    vi.mocked(memoryApi.listSummaries).mockResolvedValue({
      summaries: [
        {
          id: 'sms-1',
          session_id: 'sess-1',
          content: '讨论了 Go 微服务拆分方案',
          topics: ['Go'],
          turn_range: [0, 5],
          access_count: 0,
          created_at: null,
          has_embedding: false,
          status: 'active',
        },
      ],
      count: 1,
    })
    const wrapper = mountView()
    await flushPromises()
    await openTab(wrapper, '摘要')
    expect(wrapper.text()).toContain('讨论了 Go 微服务拆分方案')
  })

  it('「在对话中使用」跳到对话并按分类收窄注入范围', async () => {
    vi.mocked(memoryApi.listMemory).mockResolvedValue({
      entries: [makeEntry({ slot: 'preference' })],
      counts: { identity: 0, preference: 1, decision: 0, fact: 0 },
      total: 1,
      slots: [],
      organize: { running: false, started_at: null, finished_at: null, result: null, error: null },
    })
    const wrapper = mountView()
    await flushPromises()

    await router.push('/')
    const btn = wrapper
      .findAll('button')
      .find((b) => (b.attributes('title') || '').includes('在对话中使用'))
    expect(btn, '未找到「在对话中使用」按钮').toBeTruthy()
    await btn!.trigger('click')
    await flushPromises()

    expect(router.currentRoute.value.path).toBe('/chat')
    expect(router.currentRoute.value.query.memory).toBe('preference')
  })

  it('切换「含归档」会带 includeArchived 重新加载列表', async () => {
    const wrapper = mountView()
    await flushPromises()
    vi.mocked(memoryApi.listMemory).mockClear()

    const sw = wrapper.find('.ant-switch')
    expect(sw.exists()).toBe(true)
    await sw.trigger('click')
    await flushPromises()

    expect(memoryApi.listMemory).toHaveBeenCalledWith(true)
  })
})
