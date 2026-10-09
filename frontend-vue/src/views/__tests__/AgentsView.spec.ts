import { describe, it, expect, vi, beforeEach } from 'vitest'
import { mount, flushPromises } from '@vue/test-utils'
import { createRouter, createMemoryHistory } from 'vue-router'

/**
 * `AgentsView` 的冒烟网 + 两条**接线**（2026-10-09）。
 *
 * **六视图补网的最后一个**（487 → 589 → 702 → 854 → 964 → 本轮 **1190**）——
 * 补完这一轮，第 133 轮扫出的"六个视图零测试"这条洞就**闭合**了 ✓。
 *
 * **测哪两条接线**：
 * ① **列表渲染**：`loadAgents` 把 `listAgents()` 的结果**直接**赋给 `agents`（`:51`）——
 *    注意这是**第三种形状**（前三个视图分别是 `data.data`、`data.data.skills`、
 *    `data.data.knowledge_bases`）⇒ 替身必须直接返回**数组** ✓；
 * ② **绑定资源的四源拉取 + 守卫**（`:165-184`）：`loadBindingOptions` 一次拉齐
 *    知识库/技能/插件/工作流四类，且**已加载就不再拉**（`:166-170` 的提前返回）✓ ——
 *    守卫坏了会让每次打开编辑器都打四个接口 ✓。
 */

if (!window.matchMedia) {
  window.matchMedia = ((query: string) => ({
    matches: false, media: query, onchange: null,
    addListener: () => {}, removeListener: () => {},
    addEventListener: () => {}, removeEventListener: () => {}, dispatchEvent: () => false,
  })) as unknown as typeof window.matchMedia
}

const AGENT = { id: 'a-1', name: '归档助手', description: '整理归档', enabled: true, visibility: 'private' }

const apiMocks = vi.hoisted(() => {
  const arr = (): Promise<unknown> => Promise.resolve([])
  return {
    // 显式 `Promise<unknown>`：否则推断成 `never[]`，`mockResolvedValue([...])` 会被 tsc 拒掉
    listAgents: vi.fn((): Promise<unknown> => Promise.resolve([])),
    listAgentSessions: vi.fn(arr),
    listMarket: vi.fn(arr),
    listKnowledgeBases: vi.fn(arr),
    listSkillResources: vi.fn(arr),
    listPlugins: vi.fn(arr),
    listWorkflows: vi.fn(arr),
    createAgent: vi.fn(arr),
    updateAgent: vi.fn(arr),
    deleteAgent: vi.fn(arr),
    runAgent: vi.fn(arr),
    getAgentSession: vi.fn(arr),
    installMarket: vi.fn(arr),
    setAgentVisibility: vi.fn(arr),
  }
})

vi.mock('../../api', () => apiMocks)

import AgentsView from '../AgentsView.vue'

const router = createRouter({
  history: createMemoryHistory(),
  routes: [
    { path: '/', component: { template: '<div />' } },
    { path: '/agents', component: { template: '<div />' } },
  ],
})

function mountView() {
  return mount(AgentsView, { global: { plugins: [router] } })
}

describe('AgentsView（此前零测试）', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    apiMocks.listAgents.mockResolvedValue([])
    apiMocks.listAgentSessions.mockResolvedValue([])
    apiMocks.listMarket.mockResolvedValue([])
    // 四类资源都返回**非空**，守卫才会认为"已加载过"
    apiMocks.listKnowledgeBases.mockResolvedValue([{ id: 'kb-1', name: '手册' }])
    apiMocks.listSkillResources.mockResolvedValue([{ id: 'sk-1', name: '摘要' }])
    apiMocks.listPlugins.mockResolvedValue([{ name: 'fs', command: 'mcp-fs', status: 'enabled' }])
    apiMocks.listWorkflows.mockResolvedValue([{ id: 'wf-1', name: '日报' }])
  })

  it('能挂载、跑完初始加载、卸载不抛错', async () => {
    const wrapper = mountView()
    await flushPromises()

    expect(apiMocks.listAgents).toHaveBeenCalled()
    expect(apiMocks.listMarket).toHaveBeenCalled()
    expect(() => wrapper.unmount()).not.toThrow()
  })

  it('★ 列表渲染：listAgents 的返回值直接用（第三种响应形状）', async () => {
    apiMocks.listAgents.mockResolvedValue([AGENT])

    const wrapper = mountView()
    await flushPromises()

    expect(wrapper.text()).toContain('归档助手')
    wrapper.unmount()
  })

  it('★ 绑定资源四源拉齐，且已加载就不再拉（loadBindingOptions 的守卫）', async () => {
    const wrapper = mountView()
    await flushPromises()
    const vm = wrapper.vm as unknown as { loadBindingOptions: () => Promise<void> }

    await vm.loadBindingOptions()
    expect(apiMocks.listKnowledgeBases).toHaveBeenCalledTimes(1)
    expect(apiMocks.listSkillResources).toHaveBeenCalledTimes(1)
    expect(apiMocks.listPlugins).toHaveBeenCalledTimes(1)
    expect(apiMocks.listWorkflows).toHaveBeenCalledTimes(1)

    // 第二次：`:166-170` 的提前返回应拦住（四类都非空）
    await vm.loadBindingOptions()
    expect(apiMocks.listKnowledgeBases).toHaveBeenCalledTimes(1)
    expect(apiMocks.listWorkflows).toHaveBeenCalledTimes(1)
    wrapper.unmount()
  })
})
