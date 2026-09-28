import { describe, it, expect, vi, beforeEach } from 'vitest'
import { mount, flushPromises } from '@vue/test-utils'
import { createRouter, createMemoryHistory } from 'vue-router'

// jsdom 未实现 matchMedia，ant-design-vue 的栅格/断点（useBreakpoint）依赖它
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

// ── @vue-flow/* 测试替身（L3-7）──
//
// 本用例断言的是「模板市场」这条与画布实现无关的链路，所以画布只给最小替身：
//   * 组件只渲染插槽 —— 视图里 8 个具名节点插槽因此不求值，不必造真实 VueFlow 环境；
//   * useVueFlow 回吐 ref 形状的空图（视图读 getNodes.value / getEdges.value）；
//   * fitView 用 spy，便于断言"套用模板后重新布局"确实被调用。
const flowSpies = vi.hoisted(() => ({ fitView: vi.fn() }))

vi.mock('@vue-flow/core', () => {
  const nodes = { value: [] as unknown[] }
  const edges = { value: [] as unknown[] }
  return {
    VueFlow: { name: 'VueFlow', template: '<div class="vue-flow-stub"><slot /></div>' },
    Handle: { name: 'Handle', template: '<span class="handle-stub" />' },
    Position: { Left: 'left', Right: 'right', Top: 'top', Bottom: 'bottom' },
    useVueFlow: () => ({
      findNode: vi.fn(),
      addNodes: vi.fn(),
      addEdges: vi.fn(),
      removeNodes: vi.fn(),
      getNodes: nodes,
      getEdges: edges,
      getSelectedNodes: { value: [] },
      fitView: flowSpies.fitView,
    }),
  }
})
vi.mock('@vue-flow/background', () => ({ Background: { name: 'Background', template: '<div />' } }))
vi.mock('@vue-flow/controls', () => ({ Controls: { name: 'Controls', template: '<div />' } }))
vi.mock('@vue-flow/minimap', () => ({ MiniMap: { name: 'MiniMap', template: '<div />' } }))

// ── 只桩掉本用例会触发的 API 调用 ──
vi.mock('../../api', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../../api')>()
  return {
    ...actual,
    api: { get: vi.fn(), post: vi.fn(), put: vi.fn(), delete: vi.fn() },
    listAgents: vi.fn(),
    listTemplates: vi.fn(),
    useTemplate: vi.fn(),
  }
})

// 视图只用 authStore.user?.id 生成 user_id —— 给最小替身即可，省掉 pinia 插件
vi.mock('../../stores/auth', () => ({ useAuthStore: () => ({ user: { id: 'u-1', name: 'Tester' } }) }))

import WorkflowView from '../WorkflowView.vue'
import { api, listAgents, listTemplates, useTemplate } from '../../api'
import type { TemplateItem } from '../../api'

/** 一个形状合法的工作流模板：2 个节点 + 1 条边（数量断言依赖它） */
const TEMPLATE: TemplateItem = {
  id: 'tpl-1',
  type: 'workflow',
  name: '翻译流水线',
  description: '输入 → LLM → 输出',
  payload: {
    nodes: [
      { id: 'input_1', label: 'Input', node_type: 'input' },
      { id: 'llm_1', label: 'LLM', node_type: 'llm' },
    ],
    edges: [{ source_id: 'input_1', target_id: 'llm_1' }],
  },
}

const router = createRouter({
  history: createMemoryHistory(),
  routes: [
    { path: '/', component: { template: '<div />' } },
    { path: '/chat', component: { template: '<div />' } },
  ],
})

/**
 * antd Modal 的组件名是 `AModal`，而 VTU 的 stubs 是按**组件名**匹配的 —— 两个 key 都要写，
 * 只写 `Modal` 不会生效（本用例调试时正是踩在这里）。
 *
 * 另外 antd Modal 默认把内容 teleport 到 body、且关闭时不渲染内容：那样就只能靠
 * `document.body` 断言、也无法表达"点开才有列表"的因果关系。这个替身按 `open`
 * 条件渲染插槽，让「点击 → 打开 → 看到列表」整条链路可直接断言。
 */
const ModalStub = {
  props: ['open', 'title'],
  template: '<div class="modal-stub" v-if="open"><slot /></div>',
}

function mountView() {
  return mount(WorkflowView, {
    global: { plugins: [router], stubs: { Modal: ModalStub, AModal: ModalStub } },
  })
}

describe('WorkflowView 模板市场（L3-7）', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    vi.mocked(api.get).mockResolvedValue({ data: { data: [] } } as never)
    vi.mocked(api.post).mockResolvedValue({ data: { data: {} } } as never)
    vi.mocked(api.delete).mockResolvedValue({ data: {} } as never)
    vi.mocked(listAgents).mockResolvedValue([])
    vi.mocked(listTemplates).mockResolvedValue([TEMPLATE])
  })

  it('工具栏「模板」入口打开弹窗，列表按模板自带的节点/边数量渲染', async () => {
    const wrapper = mountView()
    await flushPromises()

    // 工具栏右侧第一个按钮就是模板入口（不依赖具体文案，文案随语言变）
    const trigger = wrapper.find('.toolbar-right button')
    expect(trigger.exists()).toBe(true)
    expect(wrapper.find('.modal-stub').exists()).toBe(false)

    await trigger.trigger('click')
    await flushPromises()

    expect(wrapper.find('.modal-stub').exists()).toBe(true)
    expect(listTemplates).toHaveBeenCalledWith('workflow')

    const items = wrapper.findAll('.template-item')
    expect(items).toHaveLength(1)
    expect(items[0].find('.template-name').text()).toBe('翻译流水线')

    // 数量来自模板 payload（nodes/edges 长度），不是后端另给的计数字段
    const count = items[0].find('.template-count').text()
    expect(count).toContain('2')
    expect(count).toContain('1')
  })

  it('点「使用」时该行进入 loading，成功后关闭弹窗并重新布局', async () => {
    let release: ((value: unknown) => void) | undefined
    vi.mocked(useTemplate).mockReturnValue(
      new Promise((resolve) => {
        release = resolve
      }) as never,
    )

    const wrapper = mountView()
    await flushPromises()
    await wrapper.find('.toolbar-right button').trigger('click')
    await flushPromises()

    const useBtn = wrapper.find('.template-item button')
    expect(useBtn.classes()).not.toContain('ant-btn-loading')

    await useBtn.trigger('click')
    await flushPromises()

    expect(useTemplate).toHaveBeenCalledWith('tpl-1')
    // templateUsingId === tpl.id → antd Button 进入 loading（加载期间不可重复点）
    expect(wrapper.find('.template-item button').classes()).toContain('ant-btn-loading')

    release?.({ payload: TEMPLATE.payload })
    await flushPromises()

    // 成功才关窗（失败时保留列表，用户可换一个模板）
    expect(wrapper.find('.modal-stub').exists()).toBe(false)
    expect(flowSpies.fitView).toHaveBeenCalled()
  })
})
