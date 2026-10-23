import { describe, expect, it } from 'vitest'

import { GHOST_ENTER_WINDOW_MS, classifyEnter } from '../ime'

describe('CJK 伴生 Enter 幽灵键守卫', () => {
  it('compositionend 之后 500ms 内的 Enter 判为伴生键，并要求复位时间戳', () => {
    const endAt = 10_000
    // 同时到达（数毫秒内）
    expect(classifyEnter(endAt, endAt)).toEqual({ ghost: true, nextTimestamp: 0 })
    expect(classifyEnter(endAt, endAt + 4)).toEqual({ ghost: true, nextTimestamp: 0 })
    // 边界：正好 500 内仍拦
    expect(classifyEnter(endAt, endAt + GHOST_ENTER_WINDOW_MS - 1).ghost).toBe(true)
  })

  it('超出窗口的 Enter 是用户真实意图，不拦、也不动时间戳', () => {
    const endAt = 10_000
    expect(classifyEnter(endAt, endAt + GHOST_ENTER_WINDOW_MS)).toEqual({
      ghost: false,
      nextTimestamp: endAt,
    })
    expect(classifyEnter(endAt, endAt + 3_000).ghost).toBe(false)
  })

  it('从未组合过（时间戳为 0）时一律不拦', () => {
    expect(classifyEnter(0, 0)).toEqual({ ghost: false, nextTimestamp: 0 })
    expect(classifyEnter(0, 1).ghost).toBe(false)
  })

  it('核心闭环：按 nextTimestamp 复位后，紧随的正常 Enter 不再被误吞', () => {
    const endAt = 10_000
    const first = classifyEnter(endAt, endAt + 5)
    expect(first.ghost).toBe(true)

    // 调用方必须落值 first.nextTimestamp（=0）；此时复数一次紧邻击键
    const second = classifyEnter(first.nextTimestamp, endAt + 20)
    expect(second.ghost).toBe(false) // 若忘了复位，这里会被吞掉
  })
})
