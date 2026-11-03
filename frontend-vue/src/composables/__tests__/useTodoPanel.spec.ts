import { beforeEach, describe, expect, it } from 'vitest'

import { useTodoPanel } from '../useTodoPanel'

/**
 * composable 的核心职责只有一个：**把清单按会话存放，且与会话消息流互不干扰**。
 * 这里钉住的是"生命周期"与"乱序防护"，而不是渲染。
 */
const COLLAPSED_KEY = 'chiron.todoPanel.collapsed'

describe('useTodoPanel（按会话持有清单）', () => {
  beforeEach(() => {
    window.localStorage.clear()
  })

  it('初始为空板，未设置过的会话也能安全取用', () => {
    const panel = useTodoPanel()
    expect(panel.hasTodos('s1')).toBe(false)
    expect(panel.boardOf('s1').rows).toEqual([])
  })

  it('采用事件后可按 session 读取', () => {
    const panel = useTodoPanel()
    panel.applyEvent('s1', [{ id: 't1', content: 'a', status: 'pending' }], 1)

    expect(panel.hasTodos('s1')).toBe(true)
    expect(panel.boardOf('s1').rows[0].content).toBe('a')
    // 别的会话不受影响 —— 这是"按会话隔离"的本体
    expect(panel.hasTodos('s2')).toBe(false)
  })

  it('两个会话的清单完全独立，互不覆盖', () => {
    const panel = useTodoPanel()
    panel.applyEvent('s1', [{ id: 't1', content: '甲的活', status: 'pending' }], 1)
    panel.applyEvent('s2', [{ id: 't1', content: '乙的活', status: 'pending' }], 1)

    expect(panel.boardOf('s1').rows[0].content).toBe('甲的活')
    expect(panel.boardOf('s2').rows[0].content).toBe('乙的活')
  })

  it('乱序到达的旧事件被丢弃，面板不倒退', () => {
    const panel = useTodoPanel()
    panel.applyEvent('s1', [{ id: 't1', content: '新', status: 'completed' }], 5)
    const applied = panel.applyEvent('s1', [{ id: 't1', content: '旧', status: 'pending' }], 3)

    expect(applied).toBe(false)
    expect(panel.boardOf('s1').rows[0].content).toBe('新')
    expect(panel.boardOf('s1').revision).toBe(5)
  })

  it('缺 revision 的旧事件照常采用（升级后老会话不能空白）', () => {
    const panel = useTodoPanel()
    const applied = panel.applyEvent('s1', [{ id: 't1', content: 'a', status: 'pending' }])

    expect(applied).toBe(true)
    expect(panel.hasTodos('s1')).toBe(true)
  })

  it('clear 只清指定会话，其它会话保留', () => {
    const panel = useTodoPanel()
    panel.applyEvent('s1', [{ id: 't1', content: 'a', status: 'pending' }], 1)
    panel.applyEvent('s2', [{ id: 't1', content: 'b', status: 'pending' }], 1)

    panel.clear('s1')

    expect(panel.hasTodos('s1')).toBe(false)
    expect(panel.hasTodos('s2')).toBe(true)
  })

  it('clear 未持有的会话是安全的空操作', () => {
    const panel = useTodoPanel()
    expect(() => panel.clear('never-seen')).not.toThrow()
  })

  it('折叠偏好持久化，重新创建 composable 后仍生效', () => {
    const first = useTodoPanel()
    expect(first.collapsed.value).toBe(false)
    first.toggleCollapsed()
    expect(first.collapsed.value).toBe(true)
    expect(window.localStorage.getItem(COLLAPSED_KEY)).toBe('1')

    // 新实例读回偏好（用户切页面回来时不该弹回去）
    expect(useTodoPanel().collapsed.value).toBe(true)

    first.toggleCollapsed()
    expect(useTodoPanel().collapsed.value).toBe(false)
  })

  it('localStorage 不可用时折叠仍能工作（隐私模式）', () => {
    const original = window.localStorage
    // 模拟隐私模式：读写都抛
    Object.defineProperty(window, 'localStorage', {
      value: {
        getItem() {
          throw new Error('denied')
        },
        setItem() {
          throw new Error('denied')
        },
      },
      configurable: true,
      writable: true,
    })
    try {
      const panel = useTodoPanel()
      expect(panel.collapsed.value).toBe(false)
      expect(() => panel.toggleCollapsed()).not.toThrow()
      expect(panel.collapsed.value).toBe(true)
    } finally {
      Object.defineProperty(window, 'localStorage', {
        value: original,
        configurable: true,
        writable: true,
      })
    }
  })

  it('applyEvent 传空 sessionId 时忽略，不产生幽灵条目', () => {
    const panel = useTodoPanel()
    expect(panel.applyEvent('', [{ id: 't1', content: 'a' }], 1)).toBe(false)
    expect(panel.boards.value).toEqual({})
  })

  it('引擎发来空清单时板子变空（用于清空面板事件）', () => {
    const panel = useTodoPanel()
    panel.applyEvent('s1', [{ id: 't1', content: 'a', status: 'pending' }], 1)
    panel.applyEvent('s1', [], 2)

    expect(panel.hasTodos('s1')).toBe(false)
  })
})
