import { mount } from '@vue/test-utils'
import { describe, expect, it, vi } from 'vitest'

// `Handle` 依赖 VueFlow 注入的节点上下文，单独挂载卡片时会抛错。
// 这里只验证卡片自身的渲染与事件，把连接点换成无副作用的占位元素。
vi.mock('@vue-flow/core', () => ({
  Handle: { name: 'Handle', template: '<span class="handle-stub" />' },
  Position: { Top: 'top', Bottom: 'bottom', Left: 'left', Right: 'right' },
}))

import SessionCard from '../components/SessionCard.vue'
import type { MapCard } from '../useSessionMapGraph'

function card(overrides: Partial<MapCard> = {}): MapCard {
  return {
    id: 'n1',
    sessionId: 's1',
    title: '会话标题',
    alias: '',
    tag: '',
    color: '',
    branchFromSeq: 0,
    parentTitle: '',
    childCount: 0,
    descendantCount: 0,
    collapsed: false,
    running: false,
    hasSession: true,
    x: 0,
    y: 0,
    pinned: false,
    ...overrides,
  }
}

describe('SessionCard', () => {
  it('渲染标题，无标题时回落到占位文案', () => {
    expect(mount(SessionCard, { props: { card: card() } }).text()).toContain('会话标题')

    const untitled = mount(SessionCard, { props: { card: card({ title: '', sessionId: '' }) } })
    expect(untitled.text()).toContain('未命名会话')
  })

  it('标题里的 HTML 被当文本渲染，不注入 DOM', () => {
    // 旧实现靠每处手写 escapeHtml（app.js 里 40 处）防注入，漏一处就是 XSS；
    // 模板插值天然转义，这条测试把这个保证钉住。
    const wrapper = mount(SessionCard, { props: { card: card({ title: '<img src=x onerror=alert(1)>' }) } })

    expect(wrapper.find('img').exists()).toBe(false)
    expect(wrapper.text()).toContain('<img src=x onerror=alert(1)>')
  })

  it('分叉卡片显示血缘徽标，含父会话标题与起始序号', () => {
    const wrapper = mount(SessionCard, {
      props: { card: card({ branchFromSeq: 7, parentTitle: '根会话' }) },
    })

    expect(wrapper.find('.map-card-branch').text()).toContain('根会话')
    expect(wrapper.find('.map-card-branch').text()).toContain('7')
  })

  it('父会话已删时只显示序号，不显示空标题', () => {
    const wrapper = mount(SessionCard, { props: { card: card({ branchFromSeq: 3 }) } })

    expect(wrapper.find('.map-card-branch').text()).toContain('3')
    expect(wrapper.find('.map-card-branch').text()).not.toContain('「')
  })

  it('非分叉卡片不显示血缘徽标', () => {
    expect(mount(SessionCard, { props: { card: card() } }).find('.map-card-branch').exists()).toBe(false)
  })

  it('没有子节点时不显示折叠按钮', () => {
    expect(mount(SessionCard, { props: { card: card({ childCount: 0 }) } }).find('[aria-expanded]').exists()).toBe(false)
  })

  it('有子节点时折叠按钮带 aria-expanded 与后代数量', () => {
    const wrapper = mount(SessionCard, {
      props: { card: card({ childCount: 1, descendantCount: 4 }) },
    })
    const btn = wrapper.find('[aria-expanded]')

    expect(btn.attributes('aria-expanded')).toBe('true')
    expect(btn.attributes('aria-label')).toContain('4')
  })

  it('已折叠时 aria-expanded 为 false', () => {
    const wrapper = mount(SessionCard, {
      props: { card: card({ childCount: 2, descendantCount: 2, collapsed: true }) },
    })

    expect(wrapper.find('[aria-expanded]').attributes('aria-expanded')).toBe('false')
  })

  it('点击折叠按钮发出 toggle-collapse，且不冒泡成选中卡片', () => {
    const wrapper = mount(SessionCard, {
      props: { card: card({ childCount: 1, descendantCount: 1 }) },
    })
    wrapper.find('[aria-expanded]').trigger('click')

    expect(wrapper.emitted('toggle-collapse')).toHaveLength(1)
  })

  it('运行中显示状态点；占位卡不可追问/分叉', () => {
    const running = mount(SessionCard, { props: { card: card({ running: true }) } })
    expect(running.find('.map-card-pulse').exists()).toBe(true)

    const detached = mount(SessionCard, { props: { card: card({ hasSession: false, sessionId: '' }) } })
    expect(detached.text()).toContain('未绑定会话')
    // 只剩「打开详情」一个按钮
    expect(detached.findAll('button')).toHaveLength(1)
  })

  it('只读模式下没有写操作入口', () => {
    const wrapper = mount(SessionCard, { props: { card: card(), editable: false } })

    expect(wrapper.findAll('button')).toHaveLength(1)
  })

  it('可编辑时提供追问与分叉入口', () => {
    const wrapper = mount(SessionCard, { props: { card: card(), editable: true } })

    wrapper.findAll('button').at(-2)!.trigger('click')
    wrapper.findAll('button').at(-1)!.trigger('click')

    expect(wrapper.emitted('followup')).toHaveLength(1)
    expect(wrapper.emitted('fork')).toHaveLength(1)
  })
})
