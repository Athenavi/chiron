import { beforeEach, describe, expect, it } from 'vitest'
import { createPinia, setActivePinia } from 'pinia'
import { TYPOGRAPHY_DEFAULTS, useTypographyStore } from '../typography'

const varOf = (name: string) => document.documentElement.style.getPropertyValue(name)

beforeEach(() => {
  localStorage.clear()
  document.documentElement.removeAttribute('style')
  setActivePinia(createPinia())
})

describe('typography store（阅读排版偏好）', () => {
  it('界面密度：默认档不写 data-density（等于既有几何）', () => {
    const store = useTypographyStore()
    store.setDensity('default')
    expect(document.documentElement.dataset.density).toBeUndefined()
  })

  it('界面密度：切档写 data-density，非法值回默认档', () => {
    const store = useTypographyStore()
    store.setDensity('compact')
    expect(document.documentElement.dataset.density).toBe('compact')
    store.setDensity('comfortable')
    expect(document.documentElement.dataset.density).toBe('comfortable')
    // 存档被改坏 / 上层传了脏值时不能把非法档写进 DOM：CSS 里没有对应规则，会静默失效
    store.setDensity('huge' as never)
    expect(document.documentElement.dataset.density).toBeUndefined()
  })

  it('默认值：正文排版与接入前一致，内容列宽按桌面端放宽到 960px', () => {
    const store = useTypographyStore()
    expect(store.state).toEqual(TYPOGRAPHY_DEFAULTS)
    expect(varOf('--chat-text-size')).toBe('16px')
    expect(varOf('--chat-text-leading')).toBe('1.75')
    expect(varOf('--chat-msg-gap')).toBe('calc(var(--chat-text-size) * var(--chat-text-leading) * 0.214)')
    expect(varOf('--chat-content-width')).toBe('960px')
  })

  it('改字号后写入 <html> 变量并持久化', () => {
    const store = useTypographyStore()
    store.setTextSize(18)
    expect(varOf('--chat-text-size')).toBe('18px')
    expect(JSON.parse(localStorage.getItem('chiron:typography:v1') as string).textSize).toBe(18)
  })

  it('行距与消息间距同样落到变量上', () => {
    const store = useTypographyStore()
    store.setLeading(2)
    store.setGap(14)
    expect(varOf('--chat-text-leading')).toBe('2')
    expect(varOf('--chat-msg-gap')).toBe('calc(var(--chat-text-size) * var(--chat-text-leading) * 0.500)')
  })

  it('内容宽度可调并持久化', () => {
    const store = useTypographyStore()
    store.setContentWidth(1200)
    expect(varOf('--chat-content-width')).toBe('1200px')
    expect(JSON.parse(localStorage.getItem('chiron:typography:v1') as string).contentWidth).toBe(1200)
  })

  it('越界或损坏的持久化值被夹到合法区间', () => {
    localStorage.setItem(
      'chiron:typography:v1',
      JSON.stringify({ textSize: 999, leading: 'big', gap: -5, contentWidth: 5000 }),
    )
    const store = useTypographyStore()
    expect(store.state.textSize).toBe(22)
    expect(store.state.leading).toBe(TYPOGRAPHY_DEFAULTS.leading)
    expect(store.state.gap).toBe(0)
    expect(store.state.contentWidth).toBe(1400)
  })

  it('存储内容不是 JSON 时回退默认值', () => {
    localStorage.setItem('chiron:typography:v1', '{ 不是 json')
    expect(useTypographyStore().state).toEqual(TYPOGRAPHY_DEFAULTS)
  })

  it('reset 恢复默认值', () => {
    const store = useTypographyStore()
    store.setTextSize(14)
    store.setGap(2)
    store.setContentWidth(720)
    store.reset()
    expect(store.state).toEqual(TYPOGRAPHY_DEFAULTS)
    expect(varOf('--chat-text-size')).toBe('16px')
    expect(varOf('--chat-content-width')).toBe('960px')
  })
})
