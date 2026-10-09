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

  /**
   * 护栏留痕（2026-10-09）：网关把 `guardrail_blocked` 存成 `tool_name='guardrail'` 的 tool_call
   * （`input` 是 `{"reason":"…"}`）。它此前刷新后渲染成**普通工具卡**，而实时路径只弹 toast ⇒
   * 同一条记录两种观感。现在两条路径都产出 `notice`。
   */
  it('★ 护栏留痕（tool_name=guardrail）→ notice 行而非工具卡，文案取自 reason', () => {
    const items = mergeHistory(
      [],
      [
        {
          id: 'guard_1',
          tool_name: 'guardrail',
          input: '{"reason":"输入包含不允许的指令，已拒绝本次请求"}',
          output: '',
          is_error: false,
          created_at: T,
        },
      ],
    )
    expect(items.some(i => i.kind === 'tool_call')).toBe(false)
    const notice = items.find(i => i.kind === 'notice') as any
    expect(notice?.tone).toBe('warning')
    expect(notice?.content).toBe('输入包含不允许的指令，已拒绝本次请求')
  })

  it('护栏留痕：reason 解析不出时回退到通用文案（不把 JSON 原样丢给用户）', () => {
    const items = mergeHistory(
      [],
      [
        { id: 'g_a', tool_name: 'guardrail', input: 'not-json', output: '', is_error: false, created_at: T },
        { id: 'g_b', tool_name: 'guardrail', input: '{"other":1}', output: '', is_error: false, created_at: T },
      ],
    )
    const notices = items.filter(i => i.kind === 'notice') as any[]
    expect(notices).toHaveLength(2)
    // 纯文本：原样用（引擎可能直接给一句人话）
    expect(notices[0]?.content).toBe('not-json')
    // 是 JSON 但没有 reason：回退到通用文案，而不是把 `{"other":1}` 显示出来
    expect(notices[1]?.content).not.toContain('other')
    expect(notices[1]?.content.length).toBeGreaterThan(0)
  })

  /**
   * 压缩留痕（2026-10-09）：网关把引擎的 `compaction` 事件落成 `tool_name='compaction'` 的记录，
   * 载荷是 `{before_tokens, after_tokens, saved_tokens, …}` ⇒ 渲染成 **info 通知行**（可追溯）。
   */
  it('★ 压缩留痕（tool_name=compaction）→ info 通知行，文案带压缩前/后', () => {
    const items = mergeHistory(
      [],
      [
        {
          id: 'compaction_1',
          tool_name: 'compaction',
          input: '{"before_tokens":42000,"after_tokens":18000,"saved_tokens":24000,"strategy":"auto"}',
          output: '',
          is_error: false,
          created_at: T,
        },
      ],
    )
    expect(items.some(i => i.kind === 'tool_call')).toBe(false)
    const notice = items.find(i => i.kind === 'notice') as any
    expect(notice?.tone).toBe('info')
    expect(notice?.content).toContain('42.0k')
    expect(notice?.content).toContain('18.0k')
  })

  it('压缩留痕：载荷解析不出时**不插行**（宁可没有，也不给半截信息）', () => {
    const items = mergeHistory(
      [],
      [{ id: 'c_bad', tool_name: 'compaction', input: 'not-json', output: '', is_error: false, created_at: T }],
    )
    expect(items.some(i => i.kind === 'notice')).toBe(false)
  })

  /**
   * 中断留痕（2026-10-09）：回合以 `cancelled` 收尾时网关落一条 `tool_name='interrupted'` 的记录
   * （`reason` 是 `cancelled` | `timeout`）。此前**断线 / 会话取消 / 超时**在界面上毫无痕迹 ——
   * 用户主动停止才有 `stopped` 标记，而那是纯前端字段、不入库 ⇒ 刷新即消失。
   */
  it('★ 中断留痕（tool_name=interrupted）→ warning 通知行，且超时与取消分开说', () => {
    const items = mergeHistory(
      [],
      [
        { id: 'int_1', tool_name: 'interrupted', input: '{"reason":"cancelled"}', output: '', is_error: false, created_at: T },
        { id: 'int_2', tool_name: 'interrupted', input: '{"reason":"timeout"}', output: '', is_error: false, created_at: T },
      ],
    )
    expect(items.some(i => i.kind === 'tool_call')).toBe(false)
    const notices = items.filter(i => i.kind === 'notice') as any[]
    expect(notices).toHaveLength(2)
    expect(notices.every(n => n.tone === 'warning')).toBe(true)
    // 两者文案不同（超时 vs 取消），且都不是空的
    expect(notices[0]?.content).not.toBe(notices[1]?.content)
    expect(notices[0]?.content.length).toBeGreaterThan(0)
    expect(notices[1]?.content.length).toBeGreaterThan(0)
  })
})
