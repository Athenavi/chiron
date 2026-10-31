import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest'
import { defineComponent, h } from 'vue'
import { mount, type VueWrapper } from '@vue/test-utils'

import {
  useKeyboardShortcuts,
  parseCombo,
  matchesCombo,
  eventToCombo,
  comboLabel,
  type ShortcutBinding,
} from '../useKeyboardShortcuts'

/**
 * `useKeyboardShortcuts` 的测试。
 *
 * 它替换的是 `ChatView.vue` 里那条 if 链，而那条链承载着一个**不能丢**的语义：
 * Esc 逐层关闭（停止生成 > 关浮窗 > 关侧栏）。搬到注册表后，"逐层"由**声明顺序**表达，
 * 于是顺序本身就成了契约 —— 这里逐条钉住。
 */

let wrapper: VueWrapper | null = null

function mountShortcuts(bindings: ShortcutBinding[], userId: string | null = 'u1') {
  let api!: ReturnType<typeof useKeyboardShortcuts>
  const Host = defineComponent({
    setup() {
      api = useKeyboardShortcuts({ bindings, userId })
      return () => h('div')
    },
  })
  wrapper = mount(Host, { attachTo: document.body })
  return api
}

function press(init: KeyboardEventInit & { isComposing?: boolean }) {
  const { isComposing, ...rest } = init
  const ev = new KeyboardEvent('keydown', { cancelable: true, ...rest })
  if (isComposing) Object.defineProperty(ev, 'isComposing', { value: true })
  window.dispatchEvent(ev)
  return ev
}

afterEach(() => {
  wrapper?.unmount()
  wrapper = null
})

beforeEach(() => {
  localStorage.clear()
})

describe('组合键解析', () => {
  it('mod 按平台展开：非 mac 是 ctrl，mac 是 meta', () => {
    expect(parseCombo('mod+k', false)).toMatchObject({ key: 'k', ctrl: true, meta: false })
    expect(parseCombo('mod+k', true)).toMatchObject({ key: 'k', ctrl: false, meta: true })
  })

  it('别名归一：esc → escape、空格 → space、方向键去掉 arrow 前缀', () => {
    expect(parseCombo('esc', false).key).toBe('escape')
    expect(parseCombo('space', false).key).toBe('space')
    expect(parseCombo('up', false).key).toBe('up')
  })

  it('精确匹配：多按了 shift 就不该命中 mod+k', () => {
    const combo = parseCombo('mod+k', false)
    const plain = new KeyboardEvent('keydown', { key: 'k', ctrlKey: true })
    const withShift = new KeyboardEvent('keydown', { key: 'k', ctrlKey: true, shiftKey: true })
    expect(matchesCombo(plain, combo)).toBe(true)
    expect(matchesCombo(withShift, combo)).toBe(false)
  })

  it('eventToCombo 只按下修饰键时返回空串（用户还没按完）', () => {
    expect(eventToCombo(new KeyboardEvent('keydown', { key: 'Control' }), false)).toBe('')
    expect(eventToCombo(new KeyboardEvent('keydown', { key: 'Shift' }), false)).toBe('')
  })

  it('eventToCombo 把平台修饰键记成 mod', () => {
    expect(eventToCombo(new KeyboardEvent('keydown', { key: 'k', ctrlKey: true }), false)).toBe('mod+k')
    expect(eventToCombo(new KeyboardEvent('keydown', { key: 'k', metaKey: true }), true)).toBe('mod+k')
  })

  it('comboLabel 按平台给出可读标签', () => {
    expect(comboLabel('mod+k', false)).toBe('Ctrl+K')
    expect(comboLabel('mod+k', true)).toBe('⌘K')
    expect(comboLabel('escape', false)).toBe('Esc')
  })
})

describe('注册表分发', () => {
  it('命中的绑定被调用，并阻止默认行为', () => {
    const hit = vi.fn()
    mountShortcuts([{ id: 'palette', defaultKeys: 'mod+k', handler: hit }])
    const ev = press({ key: 'k', ctrlKey: true })
    expect(hit).toHaveBeenCalledTimes(1)
    expect(ev.defaultPrevented).toBe(true)
  })

  it('未命中的绑定不触发', () => {
    const hit = vi.fn()
    mountShortcuts([{ id: 'palette', defaultKeys: 'mod+k', handler: hit }])
    press({ key: 'j', ctrlKey: true })
    expect(hit).not.toHaveBeenCalled()
  })

  it('输入法组合期间不响应', () => {
    const hit = vi.fn()
    mountShortcuts([{ id: 'palette', defaultKeys: 'mod+k', handler: hit }])
    press({ key: 'k', ctrlKey: true, isComposing: true })
    expect(hit).not.toHaveBeenCalled()
  })

  it('when 返回 false 时跳过该绑定', () => {
    const hit = vi.fn()
    mountShortcuts([{ id: 'palette', defaultKeys: 'mod+k', handler: hit, when: () => false }])
    press({ key: 'k', ctrlKey: true })
    expect(hit).not.toHaveBeenCalled()
  })
})

describe('Esc 逐层关闭（声明顺序即优先级）', () => {
  /**
   * 复刻 ChatView 的真实层级：停止生成 > 关浮窗 > 关侧栏。
   * 每一层是否"适用"由 when 表达，谁先命中谁消费。
   */
  function buildLayers(state: { loading: boolean; overlay: boolean; panel: boolean }) {
    const stopGen = vi.fn()
    const closeOverlay = vi.fn()
    const closePanel = vi.fn()
    mountShortcuts([
      { id: 'stop-generation', defaultKeys: 'escape', handler: stopGen, when: () => state.loading },
      { id: 'close-overlay', defaultKeys: 'escape', handler: closeOverlay, when: () => state.overlay },
      { id: 'close-panel', defaultKeys: 'escape', handler: closePanel, when: () => state.panel },
    ])
    return { stopGen, closeOverlay, closePanel }
  }

  it('生成中优先停止生成，其余层不动', () => {
    const { stopGen, closeOverlay, closePanel } = buildLayers({ loading: true, overlay: true, panel: true })
    press({ key: 'Escape' })
    expect(stopGen).toHaveBeenCalledTimes(1)
    expect(closeOverlay).not.toHaveBeenCalled()
    expect(closePanel).not.toHaveBeenCalled()
  })

  it('未在生成时关浮窗，不碰侧栏', () => {
    const { stopGen, closeOverlay, closePanel } = buildLayers({ loading: false, overlay: true, panel: true })
    press({ key: 'Escape' })
    expect(stopGen).not.toHaveBeenCalled()
    expect(closeOverlay).toHaveBeenCalledTimes(1)
    expect(closePanel).not.toHaveBeenCalled()
  })

  it('只剩侧栏时才关侧栏', () => {
    const { closePanel } = buildLayers({ loading: false, overlay: false, panel: true })
    press({ key: 'Escape' })
    expect(closePanel).toHaveBeenCalledTimes(1)
  })

  it('什么都不适用时无人被调用', () => {
    const { stopGen, closeOverlay, closePanel } = buildLayers({ loading: false, overlay: false, panel: false })
    press({ key: 'Escape' })
    expect(stopGen).not.toHaveBeenCalled()
    expect(closeOverlay).not.toHaveBeenCalled()
    expect(closePanel).not.toHaveBeenCalled()
  })
})

describe('改键与持久化', () => {
  const bindings: ShortcutDefLike[] = [
    { id: 'palette', defaultKeys: 'mod+k', handler: () => {} },
    { id: 'find', defaultKeys: 'mod+f', handler: () => {} },
  ]

  it('未改键时跟随默认值', () => {
    const api = mountShortcuts(bindings)
    expect(api.keysOf('palette')).toBe('mod+k')
  })

  it('改键后立即生效，并写入账号命名空间', () => {
    const api = mountShortcuts(bindings, 'u1')
    expect(api.setKeys('palette', 'mod+p')).toEqual({ ok: true })
    expect(api.keysOf('palette')).toBe('mod+p')
    expect(localStorage.getItem('chiron:shortcuts:u1')).toContain('mod+p')
  })

  it('改键跨挂载保留', () => {
    const api = mountShortcuts(bindings, 'u1')
    api.setKeys('palette', 'mod+p')
    wrapper?.unmount()

    const again = mountShortcuts(bindings, 'u1')
    expect(again.keysOf('palette')).toBe('mod+p')
  })

  it('不同账号各自独立，互不串用', () => {
    const a = mountShortcuts(bindings, 'alice')
    a.setKeys('palette', 'mod+p')
    wrapper?.unmount()

    const b = mountShortcuts(bindings, 'bob')
    expect(b.keysOf('palette')).toBe('mod+k')
  })

  it('与其它绑定冲突时拒绝写入并报出占用方', () => {
    const api = mountShortcuts(bindings, 'u1')
    expect(api.setKeys('palette', 'mod+f')).toEqual({ ok: false, conflictWith: 'find' })
    // 冲突不落库：仍是原值
    expect(api.keysOf('palette')).toBe('mod+k')
    expect(localStorage.getItem('chiron:shortcuts:u1')).toBeNull()
  })

  it('conflictOf 能查到占用方', () => {
    const api = mountShortcuts(bindings, 'u1')
    expect(api.conflictOf('palette', 'mod+f')).toBe('find')
    expect(api.conflictOf('palette', 'mod+j')).toBeNull()
  })

  it('改键后旧组合不再触发', () => {
    const hit = vi.fn()
    const api = mountShortcuts([{ id: 'palette', defaultKeys: 'mod+k', handler: hit }], 'u1')
    api.setKeys('palette', 'mod+p')

    press({ key: 'k', ctrlKey: true })
    expect(hit).not.toHaveBeenCalled()

    press({ key: 'p', ctrlKey: true })
    expect(hit).toHaveBeenCalledTimes(1)
  })

  it('resetKeys 恢复单条默认', () => {
    const api = mountShortcuts(bindings, 'u1')
    api.setKeys('palette', 'mod+p')
    api.resetKeys('palette')
    expect(api.keysOf('palette')).toBe('mod+k')
  })

  it('resetAll 恢复全部默认', () => {
    const api = mountShortcuts(bindings, 'u1')
    api.setKeys('palette', 'mod+p')
    api.setKeys('find', 'mod+l')
    api.resetAll()
    expect(api.keysOf('palette')).toBe('mod+k')
    expect(api.keysOf('find')).toBe('mod+f')
  })

  it('localStorage 里是脏数据时回落默认值而不是崩溃', () => {
    localStorage.setItem('chiron:shortcuts:u1', '{not json')
    const api = mountShortcuts(bindings, 'u1')
    expect(api.keysOf('palette')).toBe('mod+k')
  })
})

describe('definitions 快照', () => {
  it('给出可渲染的定义列表，不含 handler', () => {
    const api = mountShortcuts([
      { id: 'palette', defaultKeys: 'mod+k', handler: () => {} },
      { id: 'help', defaultKeys: 'shift+?', customizable: false, handler: () => {} },
    ])
    expect(api.definitions.value).toEqual([
      { id: 'palette', defaultKeys: 'mod+k', customizable: undefined },
      { id: 'help', defaultKeys: 'shift+?', customizable: false },
    ])
    expect('handler' in api.definitions.value[0]).toBe(false)
  })
})

/** 测试里只关心 id / defaultKeys / handler，省掉重复的 when */
type ShortcutDefLike = ShortcutBinding
