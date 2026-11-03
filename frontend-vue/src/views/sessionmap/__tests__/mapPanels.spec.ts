import { mount } from '@vue/test-utils'
import { beforeEach, describe, expect, it, vi } from 'vitest'

const mocks = vi.hoisted(() => ({
  getChatSessionMessages: vi.fn(),
  updateSessionMeta: vi.fn(),
}))

vi.mock('../../../api', () => ({
  getChatSessionMessages: mocks.getChatSessionMessages,
}))

// 注意：vi.mock 的路径按**本文件**所在目录解析 —— spec 在 __tests__/ 下，
// 上一级才是 sessionmap/，写成 ../../ 会 mock 到 src/views/useMapActions（不存在）。
vi.mock('../useMapActions', () => ({
  updateSessionMeta: mocks.updateSessionMeta,
}))

import CardInspector from '../components/CardInspector.vue'
import DraftComposer from '../components/DraftComposer.vue'
import ImportPicker from '../components/ImportPicker.vue'
import type { MapCard } from '../useSessionMapGraph'
import type { ChatSession } from '../../../components/chat/chat-types'

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

function session(id: string, title: string): ChatSession {
  return { id, title, created_at: '2026-01-01T00:00:00Z', updated_at: '2026-01-01T00:00:00Z' }
}

beforeEach(() => {
  vi.clearAllMocks()
  mocks.getChatSessionMessages.mockResolvedValue({ messages: [] })
})

describe('CardInspector', () => {
  it('统计轮次与工具调用数：tool_calls 是 JSON 字符串，要解析后计数', async () => {
    mocks.getChatSessionMessages.mockResolvedValue({
      messages: [
        { role: 'user', content: 'a' },
        { role: 'assistant', content: 'b', tool_calls: JSON.stringify([{ id: 't1' }, { id: 't2' }]) },
        { role: 'user', content: 'c' },
        { role: 'assistant', content: 'd', tool_calls: '[{"id":"t3"}]' },
      ],
    })

    const wrapper = mount(CardInspector, { props: { card: card(), editable: true } })
    await vi.waitFor(() => expect(wrapper.text()).toContain('2'))

    const values = wrapper.findAll('.map-inspector-stat dd').map(dd => dd.text())
    expect(values).toEqual(['2', '3', '4'])
  })

  it('tool_calls 不是合法 JSON 时计 0，而不是让整块面板白屏', async () => {
    mocks.getChatSessionMessages.mockResolvedValue({
      messages: [
        { role: 'assistant', content: 'x', tool_calls: 'not-json' },
        { role: 'user', content: 'y' },
      ],
    })

    const wrapper = mount(CardInspector, { props: { card: card(), editable: true } })
    await vi.waitFor(() => expect(wrapper.findAll('.map-inspector-stat dd')[1].text()).toBe('0'))
  })

  it('没有绑定会话时不发请求', () => {
    mount(CardInspector, { props: { card: card({ sessionId: '', hasSession: false }) } })

    expect(mocks.getChatSessionMessages).not.toHaveBeenCalled()
  })

  it('保存调用 updateSessionMeta 并回传保存后的值', async () => {
    mocks.updateSessionMeta.mockResolvedValue(undefined)
    const wrapper = mount(CardInspector, { props: { card: card(), editable: true } })
    await vi.waitFor(() => expect(mocks.getChatSessionMessages).toHaveBeenCalled())

    await wrapper.find('#map-inspector-alias').setValue('  备注  ')
    await wrapper.find('#map-inspector-tag').setValue('urgent')
    await wrapper.find('.map-inspector-btn.is-primary').trigger('click')

    expect(mocks.updateSessionMeta).toHaveBeenCalledWith('s1', { alias: '备注', tag: 'urgent' })
    expect(wrapper.emitted('meta-updated')?.[0]).toEqual([{ alias: '备注', tag: 'urgent' }])
  })

  it('未改动时保存按钮禁用，避免无意义的 PUT', async () => {
    const wrapper = mount(CardInspector, { props: { card: card({ alias: '旧' }), editable: true } })
    await vi.waitFor(() => expect(mocks.getChatSessionMessages).toHaveBeenCalled())

    expect(wrapper.find('.map-inspector-btn.is-primary').attributes('disabled')).toBeDefined()
  })

  it('保存失败时错误留在面板里可见（静默失败会让人以为存上了）', async () => {
    mocks.updateSessionMeta.mockRejectedValue(new Error('boom'))
    const wrapper = mount(CardInspector, { props: { card: card(), editable: true } })
    await vi.waitFor(() => expect(mocks.getChatSessionMessages).toHaveBeenCalled())

    await wrapper.find('#map-inspector-alias').setValue('x')
    await wrapper.find('.map-inspector-btn.is-primary').trigger('click')
    await vi.waitFor(() => expect(wrapper.find('[role="alert"]').exists()).toBe(true))

    expect(wrapper.find('[role="alert"]').text()).toContain('会话服务调用失败')
    expect(wrapper.emitted('meta-updated')).toBeUndefined()
  })

  it('只读模式下没有写操作入口', async () => {
    const wrapper = mount(CardInspector, { props: { card: card(), editable: false } })
    await vi.waitFor(() => expect(mocks.getChatSessionMessages).toHaveBeenCalled())

    expect(wrapper.find('#map-inspector-alias').exists()).toBe(false)
    expect(wrapper.findAll('button')).toHaveLength(1)
  })
})

describe('DraftComposer', () => {
  it('正文为空时不可提交（避免创建一张指向空会话的卡片）', () => {
    const wrapper = mount(DraftComposer)

    expect(wrapper.find('.map-draft-btn.is-primary').attributes('disabled')).toBeDefined()
  })

  it('提交时修剪首尾空白，并把标题与正文一起交出去', async () => {
    const wrapper = mount(DraftComposer)

    await wrapper.find('#map-draft-title').setValue('  新会话  ')
    await wrapper.find('#map-draft-text').setValue('  你好  ')
    await wrapper.find('.map-draft-btn.is-primary').trigger('click')

    expect(wrapper.emitted('submit')?.[0]).toEqual([{ title: '新会话', text: '你好' }])
  })

  it('Ctrl+Enter 提交，Esc 取消', async () => {
    const wrapper = mount(DraftComposer)
    await wrapper.find('#map-draft-text').setValue('内容')

    const textarea = wrapper.find('#map-draft-text')
    await textarea.trigger('keydown', { key: 'Enter', ctrlKey: true })
    await textarea.trigger('keydown', { key: 'Escape' })

    expect(wrapper.emitted('submit')).toHaveLength(1)
    expect(wrapper.emitted('cancel')).toHaveLength(1)
  })

  it('父组件忙时按钮禁用并显示加载态', () => {
    const wrapper = mount(DraftComposer, { props: { busy: true, error: '创建会话失败' } })

    expect(wrapper.find('.map-draft-btn.is-primary').attributes('disabled')).toBeDefined()
    expect(wrapper.find('[role="alert"]').text()).toBe('创建会话失败')
  })
})

describe('ImportPicker', () => {
  it('已在地图上的会话不出现在候选里（重复导入会留下删不掉的孤儿卡片）', () => {
    const wrapper = mount(ImportPicker, {
      props: { sessions: [session('s1', '一'), session('s2', '二')], existing: ['s1'] },
    })

    expect(wrapper.findAll('.map-import-item')).toHaveLength(1)
    expect(wrapper.text()).toContain('二')
    expect(wrapper.text()).not.toContain('一')
  })

  it('搜索按标题过滤', async () => {
    const wrapper = mount(ImportPicker, {
      props: { sessions: [session('s1', '部署脚本'), session('s2', '前端重构')], existing: [] },
    })

    await wrapper.find('.map-import-search').setValue('部署')

    expect(wrapper.findAll('.map-import-item')).toHaveLength(1)
    expect(wrapper.text()).toContain('部署脚本')
  })

  it('未选中时确认按钮禁用', () => {
    const wrapper = mount(ImportPicker, { props: { sessions: [session('s1', '一')], existing: [] } })

    expect(wrapper.find('.map-import-btn.is-primary').attributes('disabled')).toBeDefined()
  })

  it('勾选多人后一次性交出 id 列表', async () => {
    const wrapper = mount(ImportPicker, {
      props: { sessions: [session('s1', '一'), session('s2', '二')], existing: [] },
    })

    const boxes = wrapper.findAll('input[type="checkbox"]')
    await boxes[0].trigger('change')
    await boxes[1].trigger('change')
    await wrapper.find('.map-import-btn.is-primary').trigger('click')

    expect(wrapper.emitted('import')?.[0]).toEqual([['s1', 's2']])
  })

  it('空候选给一句提示，而不是一片空白', () => {
    const wrapper = mount(ImportPicker, { props: { sessions: [], existing: [] } })

    expect(wrapper.find('.map-import-note').text()).toBe('没有可导入的会话')
  })
})
