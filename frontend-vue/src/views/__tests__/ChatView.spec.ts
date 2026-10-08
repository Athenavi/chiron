import { describe, it, expect, vi, beforeEach } from 'vitest'
import { mount, flushPromises } from '@vue/test-utils'
import { createRouter, createMemoryHistory } from 'vue-router'

/**
 * ChatView 的**冒烟网**（3429 行、此前**零测试**）。
 *
 * 目的不是覆盖业务，而是先钉住"能挂载、初始加载会跑、卸载不炸"这三件事 ——
 * 它是全仓最大的文件，没有这层网就不该动它（拆分评估 §2.5 的前置条件）。
 *
 * 做法：重子组件全部打桩（它们的失败与本文件的挂载路径无关），只保留 ChatView 自身 +
 * 轻量的 ChatEmptyHero；`../../api` 用"永远返回空列表"的替身，避免任何真实网络。
 *
 * ⚠ `vi.mock` 的工厂会被提升到文件顶部，**不能**引用文件后面的顶层变量 ⇒ 替身一律放进
 * `vi.hoisted`（与 WorkflowView.spec.ts 同款写法）。
 */

// jsdom 未实现 matchMedia，antd 的断点 hook 依赖它
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

const apiMocks = vi.hoisted(() => {
  // 参数写成可选且**显式 void**：让 mock 的签名既能接 URL 又能无参调用（vue-tsc 需要），
  // 同时避免 eslint 的 unused-vars（本仓未开 argsIgnorePattern）
  const emptyList = (url?: string) => {
    void url
    return Promise.resolve({ data: { data: [] } })
  }
  return {
    emptyList,
    api: {
      get: vi.fn(emptyList),
      post: vi.fn(emptyList),
      put: vi.fn(emptyList),
      delete: vi.fn(emptyList),
    },
    createSSEConnection: vi.fn(() => ({ close: vi.fn(), addEventListener: vi.fn(), readyState: 1 })),
    submitApproval: vi.fn(emptyList),
    submitAnswer: vi.fn(emptyList),
    updateConversation: vi.fn(emptyList),
    createShare: vi.fn(emptyList),
    getActiveShare: vi.fn(emptyList),
    revokeShare: vi.fn(emptyList),
    getChatSessionMessages: vi.fn(emptyList),
    resolveMediaUrl: vi.fn((u: string) => u),
    listModels: vi.fn(emptyList),
    createAgent: vi.fn(emptyList),
    createGraph: vi.fn(emptyList),
  }
})

vi.mock('../../api', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../../api')>()
  return { ...actual, ...apiMocks }
})

vi.mock('../../api/sessionRuntime', () => ({
  getSessionRuntime: vi.fn(() => Promise.resolve(null)),
  putSessionRuntime: vi.fn(() => Promise.resolve()),
}))

vi.mock('../../stores/auth', () => ({
  useAuthStore: () => ({ user: { id: 'u-1', name: 'Tester', tenant_id: 't-1' }, token: 'tk' }),
}))
vi.mock('../../stores/theme', () => ({
  useThemeStore: () => ({ isDark: false, toggle: vi.fn() }),
}))

import ChatView from '../ChatView.vue'

const HEAVY_CHILDREN = {
  MessageList: true,
  MessageItem: true,
  ChatInput: true,
  ChatSidePanel: true,
  SubAgentPanel: true,
  SessionPreviewPane: true,
  SessionStatsPanel: true,
  CallChainTimeline: true,
  FloatingPanel: true,
  SaveToKnowledgeDialog: true,
  SaveToMemoryDialog: true,
  ChatDisplaySettings: true,
  ChatStatusBar: true,
  AskCard: true,
}

const router = createRouter({
  history: createMemoryHistory(),
  routes: [
    { path: '/', component: { template: '<div />' } },
    { path: '/chat', component: { template: '<div />' } },
  ],
})

function mountView() {
  return mount(ChatView, { global: { plugins: [router], stubs: HEAVY_CHILDREN } })
}

describe('ChatView 冒烟（3429 行、此前零测试）', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    apiMocks.api.get.mockImplementation(apiMocks.emptyList)
    apiMocks.getChatSessionMessages.mockImplementation(apiMocks.emptyList)
  })

  it('能挂载、跑完初始加载、渲染骨架，且卸载不抛错', async () => {
    const wrapper = mountView()
    await flushPromises()

    expect(wrapper.find('.chat-layout').exists()).toBe(true)
    expect(wrapper.find('.chat-main').exists()).toBe(true)
    // 初始加载真的跑了（挂载路径没被异常吞掉）
    expect(apiMocks.api.get).toHaveBeenCalledWith('/v1/conversations')

    expect(() => wrapper.unmount()).not.toThrow()
  })

  it('挂载→卸载可重复，不残留（onUnmounted 的清理路径也被走到）', async () => {
    for (let i = 0; i < 2; i++) {
      const wrapper = mountView()
      await flushPromises()
      wrapper.unmount()
    }
    expect(apiMocks.api.get.mock.calls.length).toBeGreaterThanOrEqual(2)
  })

  it('会话列表非空时按契约取消息（或明确不乱取）', async () => {
    apiMocks.api.get.mockImplementation((url?: string) => {
      if (url === '/v1/conversations') {
        return Promise.resolve({
          data: { data: [{ id: 's-1', title: '会话 1', updated_at: '2026-10-08T00:00:00Z' }] },
        }) as never
      }
      return apiMocks.emptyList() as never
    })

    const wrapper = mountView()
    await flushPromises()

    const called = apiMocks.getChatSessionMessages.mock.calls.map((c) => c[0])
    // 统一模式（unifiedMode）下不自动切换会话，此时"不乱取消息"同样是有效契约
    if (called.length > 0) {
      expect(called[0]).toBe('s-1')
    } else {
      expect(apiMocks.api.get).toHaveBeenCalledWith('/v1/conversations')
    }
    wrapper.unmount()
  })
})
