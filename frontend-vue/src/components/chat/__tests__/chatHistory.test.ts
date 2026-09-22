import { describe, expect, it } from 'vitest'
import { mergeHistory, normalizeMeta } from '../chat-history'
import { splitThinking } from '../chat-types'

const T = '2026-09-22T10:00:00Z'

/**
 * `mergeHistory` 是主区域与分屏**共用**的历史→条目管线。
 *
 * 这些用例存在的理由是一次真实缺陷：分屏参考栏曾经自己手写映射（只取 content），
 * 于是**内部格式标签直接泄露到界面**、工具调用全部丢失。所以这里既验证行为，
 * 也验证"它确实用的是与主区域同一个 `splitThinking`"，而不是另写一套。
 */
describe('mergeHistory —— 主区域与分屏共用的历史管线', () => {
  it('★ 思维链剥离：正文与 reasoning 分开，正文里不含 thinking 标签', () => {
    // 真实标记是 [thinking] / [/thinking]（见 chat-types.ts 的 THINK_START/THINK_END）
    const content = '前言\n[thinking]\n内部推理\n[/thinking]\n正文结论'
    const { reasoning, body } = splitThinking(content, { loose: true })

    const items = mergeHistory(
      [{ id: 'm1', role: 'assistant', content, created_at: T }],
      [],
    )
    const texts = items.filter(i => i.kind === 'text').map((i: any) => i.content)
    const reasons = items.filter(i => i.kind === 'reasoning').map((i: any) => i.content)

    // 与 splitThinking 的结果一致（即：没有另写一套映射）
    expect(texts.join('')).toBe(body)
    expect(reasons.join('')).toBe(reasoning)
    // 关键断言：正文里不该残留任何内部标签
    expect(texts.join('')).not.toContain('thinking')
    expect(texts.join('')).not.toContain('<')
  })

  it('★ 没有思考块时，正文原样保留且不产生 reasoning 条目', () => {
    const items = mergeHistory(
      [{ id: 'm2', role: 'assistant', content: '就是一段普通回答', created_at: T }],
      [],
    )
    const texts = items.filter(i => i.kind === 'text').map((i: any) => i.content)
    expect(texts.join('')).toBe('就是一段普通回答')
    expect(items.some(i => i.kind === 'reasoning')).toBe(false)
  })

  it('用户消息走 stripUserInputTag（不泄露输入包装标记）', () => {
    const items = mergeHistory(
      [{ id: 'm3', role: 'user', content: '你好', created_at: T }],
      [],
    )
    const first = items[0] as any
    expect(first.kind).toBe('text')
    expect(first.role).toBe('user')
    expect(first.content).toBe('你好')
  })

  it('工具调用 → tool_call / tool_result 条目（分屏曾完全丢失这些）', () => {
    const items = mergeHistory(
      [],
      [
        {
          id: 'tc1',
          tool_name: 'read_file',
          input: '{"path":"a.txt"}',
          output: 'file body',
          is_error: false,
          created_at: T,
        },
      ],
    )
    const call = items.find(i => i.kind === 'tool_call') as any
    const result = items.find(i => i.kind === 'tool_result') as any
    expect(call?.name).toBe('read_file')
    expect(result?.content).toBe('file body')
  })

  it('assistant 内联 tool_calls（字符串形式）也会被收进时间线', () => {
    const items = mergeHistory(
      [
        {
          id: 'm4',
          role: 'assistant',
          content: '我来看一下',
          created_at: T,
          tool_calls: '[{"id":"tc9","function":{"name":"grep_files","arguments":"{}"}}]',
        },
      ],
      [],
    )
    expect(items.some(i => i.kind === 'tool_call' && (i as any).name === 'grep_files')).toBe(true)
  })

  it('normalizeMeta：字符串 JSON 解析、非法值返回 undefined', () => {
    expect(normalizeMeta('{"a":1}')).toEqual({ a: 1 })
    expect(normalizeMeta({ b: 2 })).toEqual({ b: 2 })
    expect(normalizeMeta('not json')).toBeUndefined()
    expect(normalizeMeta(null)).toBeUndefined()
    expect(normalizeMeta(42)).toBeUndefined()
  })
})
