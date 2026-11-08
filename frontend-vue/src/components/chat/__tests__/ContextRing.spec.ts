import { describe, expect, it } from 'vitest'
import { mount } from '@vue/test-utils'
import ContextRing from '../ContextRing.vue'

const ring = (used: number | null, limit: number | null) =>
  mount(ContextRing, { props: { used, limit } })
// 组件根是 antd Popover；data-tone / title 的可见契约挂在内部按钮上。
const ringEl = (wrapper: ReturnType<typeof ring>) => wrapper.find('.ctx-ring')

describe('ContextRing（上下文占用环）', () => {
  it('按已用/上限给出百分比', () => {
    const wrapper = ring(50000, 100000)
    expect(wrapper.find('.ctx-label').text()).toBe('50%')
    expect(ringEl(wrapper).attributes('data-tone')).toBe('ok')
    expect(ringEl(wrapper).attributes('title')).toContain('50000 tokens / 上限 100000')
  })

  it('高占用切换色调：70% 起 warn，90% 起 danger', () => {
    expect(ringEl(ring(75000, 100000)).attributes('data-tone')).toBe('warn')
    expect(ringEl(ring(95000, 100000)).attributes('data-tone')).toBe('danger')
  })

  it('超出上限时封顶 100%，不画出超过一圈的弧', () => {
    const wrapper = ring(250000, 100000)
    expect(wrapper.find('.ctx-label').text()).toBe('100%')
    expect(ringEl(wrapper).attributes('data-tone')).toBe('danger')
  })

  it('没有已用数据或没有上限时只显示占位（不猜）', () => {
    for (const wrapper of [ring(null, 100000), ring(1200, null), ring(1200, 0)]) {
      expect(wrapper.find('.ctx-label').text()).toBe('—')
      expect(ringEl(wrapper).attributes('data-tone')).toBe('idle')
      expect(ringEl(wrapper).attributes('title')).toContain('暂无数据')
    }
  })

  it('弧长与比例一致', () => {
    const wrapper = ring(25000, 100000)
    const circ = 2 * Math.PI * 8
    const dasharray = wrapper.find('.ctx-bar').attributes('stroke-dasharray') ?? ''
    const dash = Number(dasharray.split(' ')[0])
    expect(dash).toBeCloseTo(circ * 0.25, 5)
  })
})
