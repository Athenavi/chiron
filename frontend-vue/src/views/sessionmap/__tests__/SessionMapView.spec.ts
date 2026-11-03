import { beforeEach, describe, expect, it, vi } from 'vitest'
import { defineComponent } from 'vue'
import { mount } from '@vue/test-utils'

/**
 * 外壳与宿主的契约。
 *
 * 这一层是阶段 4 的接线点，也是旧 iframe 版最容易静默失效的地方：旧实现靠
 * `postMessage` 双向 RPC，宿主任一侧漏实现一条回执，用户就要空等 20 秒。
 * 现在只剩三个 emit，所以把它们的**方向**钉死：
 *   - `close`           → 宿主收起浮层
 *   - `switch-session`  → 宿主切会话（地图自己不改宿主的 activeSessionId）
 *   - `refresh-sessions`→ 宿主重拉列表（卡片元数据改过）
 *
 * 不测画布交互本身：`buildMapGraph` 有独立单测，Vue Flow 的拖动/缩放属库的行为。
 * 这里 stub 掉 `MapCanvas`，避免把库的 `ResizeObserver` 依赖带进单测。
 */
const mocks = vi.hoisted(() => ({
  load: vi.fn(),
  select: vi.fn(),
  removeActive: vi.fn(),
  addNode: vi.fn(),
  dropNode: vi.fn(),
  dragNode: vi.fn(),
  collapseNode: vi.fn(),
  replaceNodes: vi.fn(),
  sendMessage: vi.fn(),
  createSession: vi.fn(),
  forkSession: vi.fn(),
  requestForkNode: vi.fn(),
  setNodes: null as unknown as (list: unknown[]) => void,
}))

vi.mock('../useSessionMapWorkspace', async () => {
  const { ref } = await import('vue')
  const nodes = ref<unknown[]>([])
  mocks.setNodes = (list) => {
    nodes.value = list
  }
  return {
    useSessionMapWorkspace: () => ({
      workspaces: ref([{ id: 'w1', name: '主画布' }]),
      activeId: ref('w1'),
      nodes,
      loading: ref(false),
      loadError: ref(''),
      saveError: ref(''),
      load: mocks.load,
      select: mocks.select,
      removeActive: mocks.removeActive,
      addNode: mocks.addNode,
      dropNode: mocks.dropNode,
      dragNode: mocks.dragNode,
      collapseNode: mocks.collapseNode,
      replaceNodes: mocks.replaceNodes,
    }),
  }
})

vi.mock('../api', () => ({ requestForkNode: mocks.requestForkNode }))
vi.mock('../useMapActions', () => ({
  createSession: mocks.createSession,
  forkSession: mocks.forkSession,
  sendMessage: mocks.sendMessage,
}))

import SessionMapView from '../SessionMapView.vue'

/** 只保留"能发 select-card"这一件事；其余是库的行为，不在单测范围。 */
const MapCanvasStub = defineComponent({
  name: 'MapCanvas',
  props: {
    cards: { type: Array, default: () => [] },
    edges: { type: Array, default: () => [] },
    readonly: { type: Boolean, default: false },
    activeId: { type: String, default: '' },
  },
  emits: ['move-card', 'select-card', 'toggle-collapse', 'open-detail', 'fork', 'followup'],
  template: `
    <div class="canvas-stub">
      <button class="stub-select" @click="$emit('select-card', String(cards[0]?.id ?? ''))" />
      <button class="stub-followup" @click="$emit('followup', String(cards[0]?.id ?? ''))" />
    </div>
  `,
})

const SESSIONS = [
  { id: 's1', title: '会话一', created_at: '2026-10-01T00:00:00Z', updated_at: '2026-10-01T00:00:00Z' },
]
const ONE_NODE = [{ id: 'n1', session_id: 's1', x: 80, y: 80 }]

function mountView() {
  // 只 stub 用到的两个面板，其余组件（草稿/导入）按需渲染会很吵
  return mount(SessionMapView, {
    props: { sessions: SESSIONS as never },
    global: {
      stubs: { MapCanvas: MapCanvasStub, ThreadDetail: true, DraftComposer: true, ImportPicker: true },
    },
  })
}

describe('SessionMapView 与宿主的契约', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    mocks.setNodes([])
  })

  it('关掉浮层时只 emit close，不自己去改宿主的导航状态', async () => {
    const wrapper = mountView()

    const close = wrapper.findAll('button').find(b => b.text() === '关闭')
    expect(close, '外框必须有关闭入口').toBeTruthy()
    await close!.trigger('click')

    expect(wrapper.emitted('close')).toHaveLength(1)
    expect(wrapper.emitted('switch-session')).toBeUndefined()
  })

  it('"刷新画布"重新拉后端快照，而不是只清本地状态', async () => {
    const wrapper = mountView()
    mocks.load.mockClear() // 挂载时 setup 已经拉过一次

    const refresh = wrapper.findAll('button').find(b => b.text() === '重新加载')
    expect(refresh, '外框必须有刷新入口').toBeTruthy()
    await refresh!.trigger('click')

    expect(mocks.load).toHaveBeenCalledTimes(1)
  })

  it('"整理布局"把排布结果整体写回，而不是逐个拖动（避免触发多轮防抖保存）', async () => {
    mocks.setNodes(ONE_NODE)
    const wrapper = mountView()

    const tidy = wrapper.findAll('button').find(b => b.text() === '整理布局')
    await tidy!.trigger('click')

    expect(mocks.replaceNodes).toHaveBeenCalledTimes(1)
    expect(mocks.dragNode).not.toHaveBeenCalled()
  })

  it('从画布移除卡片只删节点，不删会话 —— 会话的主人是宿主', async () => {
    mocks.setNodes(ONE_NODE)
    const wrapper = mountView()

    await wrapper.find('.stub-select').trigger('click')
    const archive = wrapper.findAll('button').find(b => b.text() === '从画布移除')
    expect(archive, '选中卡片后应出现"从画布移除"').toBeTruthy()
    await archive!.trigger('click')

    expect(mocks.dropNode).toHaveBeenCalledWith('n1')
    // 关键：不得调用任何会删会话的入口
    expect(mocks.removeActive).not.toHaveBeenCalled()
    expect(wrapper.emitted('switch-session')).toBeUndefined()
  })
})
