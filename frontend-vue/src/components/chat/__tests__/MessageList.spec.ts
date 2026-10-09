import { describe, it, expect, vi } from 'vitest'
import { mount } from '@vue/test-utils'
import MessageList from '../MessageList.vue'
import MessageItem from '../MessageItem.vue'
import type { ChatItem } from '../chat-types'

// 回归保护（7b8a218）：MessageList 曾把「所有 item 交给 MessageItem 按 kind 分发」
// 改成只放行 text/reasoning，导致 tool_call / tool_result / turn_stats / date_divider
// 被静默丢弃 —— 表现为聊天页除首条 AI 文本外内容大量缺失。
//
// ⚠ 这三个用例一律 `loading: true`（2026-10-09）：它们测的是**按 kind 的分发**，
// 而 §4.7 修好之后「活跃回合」改由**生命周期**判定（不再只看位置）⇒ `loading: false`
// 意味着该轮**已完成**，其工具组按设计**默认收起**，tool 行会被折叠头剔掉、数量对不上。
// 想看全部行就必须让该轮处于"仍在运行"态。
const items: ChatItem[] = [
  { kind: 'date_divider', content: '9月11日', id: 'd1' },
  { kind: 'text', role: 'user', content: '你好', id: 'm1' },
  { kind: 'reasoning', content: '先想一下', id: 'm2:r' },
  { kind: 'text', role: 'assistant', content: '回答', id: 'm2' },
  { kind: 'tool_call', id: 'tc1', name: 'read_file', arguments: '{}', status: 'done' },
  { kind: 'tool_result', toolCallId: 'tc1', id: 'tc1:res', content: '{"ok":true}', isError: false },
  { kind: 'turn_stats', inputTokens: 10, outputTokens: 20, durationSec: 3 },
]

describe('MessageList', () => {
  it('按 kind 渲染全部 item（不丢失 tool_call/tool_result/turn_stats/date_divider）', () => {
    const wrapper = mount(MessageList, { props: { items, loading: true } })

    // 每个非 kb_hits 的 item 都必须实例化一个 MessageItem（原回归即此处丢项）
    expect(wrapper.findAllComponents(MessageItem)).toHaveLength(items.length)

    expect(wrapper.find('.date-divider').exists()).toBe(true)
    expect(wrapper.find('.reasoning-row').exists()).toBe(true)
    expect(wrapper.find('.tool-row-wrap').exists()).toBe(true)
    expect(wrapper.find('.tool-result').exists()).toBe(true)
    expect(wrapper.find('.turn-stats').exists()).toBe(true)
    // 用户/助手文本各自渲染
    expect(wrapper.findAll('.msg-row')).toHaveLength(2)
  })

  it('kb_hits 渲染为专属标签且不占用消息项', () => {
    const withKb = [...items, { kind: 'kb_hits', count: 2, kb_id: 'kb1', id: 'k1' } as unknown as ChatItem]
    const wrapper = mount(MessageList, { props: { items: withKb, loading: true } })

    expect(wrapper.findAllComponents(MessageItem)).toHaveLength(items.length)
    expect(wrapper.find('.kb-hits-tag').text()).toContain('引用了知识库')
  })

  it('相同 id 的 reasoning/text/tool_result 不产生重复 key（列表不错位）', () => {
    const shared: ChatItem[] = [
      { kind: 'reasoning', content: '思考', id: 'same' },
      { kind: 'text', role: 'assistant', content: '正文', id: 'same' },
      { kind: 'tool_result', toolCallId: 'same', id: 'same:res', content: 'r', isError: false },
      { kind: 'turn_stats', inputTokens: 1, outputTokens: 2 },
    ]
    const warn = vi.spyOn(console, 'warn').mockImplementation(() => {})
    const wrapper = mount(MessageList, { props: { items: shared, loading: true } })

    expect(wrapper.findAllComponents(MessageItem)).toHaveLength(shared.length)
    expect(warn.mock.calls.flat().join(' ')).not.toContain('Duplicate keys')
    warn.mockRestore()
  })
})
