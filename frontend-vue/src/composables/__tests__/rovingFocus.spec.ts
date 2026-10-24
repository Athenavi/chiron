import { describe, it, expect } from 'vitest'
import { nextRovingIndex, resolveRovingIntent, rovingTabIndex } from '../useRovingFocus'

describe('roving focus（漫游焦点）', () => {
  it('键位到语义的映射与手册一致', () => {
    expect(resolveRovingIntent('ArrowRight')).toBe('next')
    expect(resolveRovingIntent('ArrowDown')).toBe('next')
    expect(resolveRovingIntent('ArrowLeft')).toBe('prev')
    expect(resolveRovingIntent('ArrowUp')).toBe('prev')
    expect(resolveRovingIntent('Home')).toBe('first')
    expect(resolveRovingIntent('PageUp')).toBe('first')
    expect(resolveRovingIntent('End')).toBe('last')
    expect(resolveRovingIntent('PageDown')).toBe('last')
    // 非导航键一律不接管（Tab 也在这里被放行 —— 键盘陷阱的来源就是错误地拦截它）
    expect(resolveRovingIntent('Tab')).toBe(null)
    expect(resolveRovingIntent('a')).toBe(null)
  })

  it('RTL 下左右镜像、上下不变', () => {
    expect(resolveRovingIntent('ArrowLeft', 'rtl')).toBe('next')
    expect(resolveRovingIntent('ArrowRight', 'rtl')).toBe('prev')
    expect(resolveRovingIntent('ArrowUp', 'rtl')).toBe('prev')
    expect(resolveRovingIntent('ArrowDown', 'rtl')).toBe('next')
  })

  it('环回与夹边两种边界行为', () => {
    expect(nextRovingIndex(0, 4, 'prev', true)).toBe(3)
    expect(nextRovingIndex(3, 4, 'next', true)).toBe(0)
    expect(nextRovingIndex(0, 4, 'prev', false)).toBe(0)
    expect(nextRovingIndex(3, 4, 'next', false)).toBe(3)
    expect(nextRovingIndex(2, 4, 'first')).toBe(0)
    expect(nextRovingIndex(2, 4, 'last')).toBe(3)
    // 空组不返回越界下标
    expect(nextRovingIndex(0, 0, 'next')).toBe(-1)
  })

  it('只有当前项占 Tab 停靠点', () => {
    expect(rovingTabIndex(0, 0)).toBe(0)
    expect(rovingTabIndex(1, 0)).toBe(-1)
    expect(rovingTabIndex(3, 3)).toBe(0)
  })
})
