/**
 * SessionPreviewPane 分栏宽度：拖拽 / 键盘 / 本地记忆 / 重置。
 *
 * 测试重点不是像素尺寸，而是**行为契约**：
 *  1. 没记忆过宽度 → 不写 inline width（保持"内容决定宽度"的现状，零回归）；
 *  2. 拖动方向随手柄贴边（handleSide）而反；
 *  3. 松手才写 localStorage（拖动过程中不该反复写）；
 *  4. 双击重置 → 清掉记忆并回到内容决定宽度；
 *  5. ←/→ 键盘微调同样持久化。
 *
 * 注意：jsdom 没有 PointerEvent，且 MouseEvent.clientX 是只读 getter，
 * 因此这里**自建事件 + defineProperty 覆盖自有属性**，不走 @vue/test-utils 的 trigger 传参。
 */
import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest'
import { nextTick } from 'vue'
import { mount } from '@vue/test-utils'

import SessionPreviewPane from '../SessionPreviewPane.vue'

vi.mock('../../../api', () => ({
  api: { get: vi.fn(async () => ({ data: { data: { messages: [] } } })) },
}))

const WIDTH_KEY = 'chiron:split-width:v1'
/** jsdom 里 getBoundingClientRect 全为 0，拖拽起点会退化成 0 —— 固定成 400 便于精确断言 */
const BASE_WIDTH = 400
const MIN_WIDTH = 260

function mountPane(props: Record<string, unknown> = {}) {
  return mount(SessionPreviewPane, {
    props: { sessionId: 's1', handleSide: 'right', ...props },
    global: { mocks: { $t: (key: string) => key } },
  })
}

function inlineWidth(wrapper: ReturnType<typeof mountPane>): string | undefined {
  return (wrapper.find('.preview-pane').element as HTMLElement).style.width || undefined
}

/** 派发一个带 clientX / pointerId 的指针事件（自有属性遮蔽只读 getter） */
async function firePointer(el: Element, type: string, clientX: number) {
  const ev = new MouseEvent(type, { bubbles: true, cancelable: true })
  Object.defineProperty(ev, 'clientX', { value: clientX })
  Object.defineProperty(ev, 'pointerId', { value: 1 })
  el.dispatchEvent(ev)
  await nextTick()
}

/** 一次完整拖拽：按下 → 移动 → 松开 */
async function drag(wrapper: ReturnType<typeof mountPane>, from: number, to: number) {
  const el = wrapper.find('.pp-resize').element
  await firePointer(el, 'pointerdown', from)
  await firePointer(el, 'pointermove', to)
  await firePointer(el, 'pointerup', to)
}

let rectSpy: ReturnType<typeof vi.spyOn>

beforeEach(() => {
  localStorage.clear()
  rectSpy = vi.spyOn(HTMLElement.prototype, 'getBoundingClientRect').mockReturnValue({
    width: BASE_WIDTH, height: 600, top: 0, left: 0, right: BASE_WIDTH, bottom: 600, x: 0, y: 0,
    toJSON: () => ({}),
  } as DOMRect)
})

afterEach(() => {
  rectSpy.mockRestore()
  localStorage.clear()
})

describe('分栏宽度', () => {
  it('没有本地记忆时不设宽度（保持现状）', () => {
    const wrapper = mountPane()
    expect(inlineWidth(wrapper)).toBeUndefined()
    expect(wrapper.find('.pp-resize').exists()).toBe(true)
  })

  it('有本地记忆时渲染即恢复宽度', () => {
    localStorage.setItem(WIDTH_KEY, '520')
    const wrapper = mountPane()
    expect(inlineWidth(wrapper)).toBe('520px')
  })

  it('右侧手柄向右拖动变宽，松手才落盘', async () => {
    const wrapper = mountPane({ handleSide: 'right' })
    const el = wrapper.find('.pp-resize').element
    await firePointer(el, 'pointerdown', 500)
    await firePointer(el, 'pointermove', 560)
    expect(inlineWidth(wrapper)).toBe(`${BASE_WIDTH + 60}px`)
    // 拖动过程中不该写 localStorage（避免每帧一次 IO）
    expect(localStorage.getItem(WIDTH_KEY)).toBeNull()
    await firePointer(el, 'pointerup', 560)
    expect(localStorage.getItem(WIDTH_KEY)).toBe(String(BASE_WIDTH + 60))
  })

  it('左侧手柄方向相反（向左拖变宽）', async () => {
    const wrapper = mountPane({ handleSide: 'left' })
    expect(wrapper.find('.pp-resize').classes()).toContain('handle-left')
    await drag(wrapper, 500, 440)
    expect(localStorage.getItem(WIDTH_KEY)).toBe(String(BASE_WIDTH + 60))
  })

  it('过窄时被下限兜住', async () => {
    const wrapper = mountPane()
    await drag(wrapper, 500, 0)
    expect(Number(localStorage.getItem(WIDTH_KEY))).toBe(MIN_WIDTH)
  })

  it('键盘 ←/→ 微调并持久化', async () => {
    const wrapper = mountPane({ handleSide: 'right' })
    const handle = wrapper.find('.pp-resize')
    await handle.trigger('keydown', { key: 'ArrowRight' })
    expect(inlineWidth(wrapper)).toBe(`${BASE_WIDTH + 16}px`)
    expect(localStorage.getItem(WIDTH_KEY)).toBe(String(BASE_WIDTH + 16))
    // 非方向键不该改动任何东西
    await handle.trigger('keydown', { key: 'a' })
    expect(inlineWidth(wrapper)).toBe(`${BASE_WIDTH + 16}px`)
  })

  it('双击重置：清掉记忆并回到内容决定宽度', async () => {
    localStorage.setItem(WIDTH_KEY, '520')
    const wrapper = mountPane()
    expect(inlineWidth(wrapper)).toBe('520px')
    await wrapper.find('.pp-resize').trigger('dblclick')
    expect(localStorage.getItem(WIDTH_KEY)).toBeNull()
    expect(inlineWidth(wrapper)).toBeUndefined()
  })

  it('损坏的本地记忆被忽略（退回内容决定宽度）', () => {
    localStorage.setItem(WIDTH_KEY, 'not-a-number')
    expect(inlineWidth(mountPane())).toBeUndefined()
  })

  it('手柄具备 separator 语义（键盘可达）', () => {
    const handle = mountPane().find('.pp-resize')
    expect(handle.attributes('role')).toBe('separator')
    expect(handle.attributes('aria-orientation')).toBe('vertical')
    expect(handle.attributes('tabindex')).toBe('0')
  })
})
