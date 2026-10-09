import { describe, it, expect } from 'vitest'
import { countItemsAfter, mergeTurnStats, splitThinking, throttleRaf } from '../chat-types'
import type { ChatItem } from '../chat-types'

// 回归保护：引擎（python-engine/app/agent/runtime.py）按 ~80 字一段下发
// "[thinking]片段[/thinking]"，因此流式 buffer 与落库文本都会出现多段思考块。
// 旧 loose 实现用「配对提取 + 孤立标签剥离」，非 loose 实现只看开头，
// 两者在第二段起都会把 "[/thinking][thinking]" 残留在正文里。
/**
 * `throttleRaf`（2026-10-09 起被 `MessageList` 的更新路径真正用上，此前**零调用点**）。
 *
 * 用**受控的 rAF 替身**测：真 jsdom 的 rAF 时序与浏览器不同（实测在 `await nextTick()`
 * 之间就会落地），在那里"同一帧内合并"**根本观察不到** —— 所以这里直接控制帧边界。
 * 这也解释了为什么 §4.3 后半**不适合**用组件级测试来钉：可观察性取决于运行环境的帧模型。
 */
describe('throttleRaf（按帧合并）', () => {
  it('★ 同一帧内多次调用只执行一次，且用最后一次的参数', () => {
    const frames: Array<() => void> = []
    const original = globalThis.requestAnimationFrame
    globalThis.requestAnimationFrame = ((cb: FrameRequestCallback) => {
      frames.push(() => cb(0))
      return frames.length
    }) as typeof requestAnimationFrame

    try {
      const seen: number[] = []
      const fn = throttleRaf((n: number) => { seen.push(n) })
      fn(1)
      fn(2)
      fn(3)
      expect(seen).toEqual([])          // 还没到帧边界 ⇒ 一次都没跑
      expect(frames).toHaveLength(1)    // 只排了**一个**帧任务（这就是"合并"）
      frames[0]!()
      expect(seen).toEqual([3])         // 帧到了 ⇒ 只跑一次，取最后一次参数
      fn(4)
      expect(frames).toHaveLength(2)    // 新的一帧重新开始排
      frames[1]!()
      expect(seen).toEqual([3, 4])
    } finally {
      globalThis.requestAnimationFrame = original
    }
  })
})

describe('splitThinking（loose 状态机）', () => {
  it('多段思考块全部归 reasoning，正文不残留标签', () => {
    const { reasoning, body } = splitThinking('[thinking]想a[/thinking][thinking]想b[/thinking]最终回答', { loose: true })
    expect(reasoning).toBe('想a想b')
    expect(body).toBe('最终回答')
    expect(body).not.toContain('thinking')
  })

  it('末段未闭合时仍算 reasoning（流式中）', () => {
    const { reasoning, body } = splitThinking('[thinking]想a[/thinking][thinking]还在想', { loose: true })
    expect(reasoning).toBe('想a还在想')
    expect(body).toBe('')
  })

  it('纯正文原样返回', () => {
    const { reasoning, body } = splitThinking('只有正文', { loose: true })
    expect(reasoning).toBe('')
    expect(body).toBe('只有正文')
  })

  it('思考块之间/之后无正文时不产生空正文项', () => {
    const { reasoning, body } = splitThinking('[thinking]a[/thinking][thinking]b[/thinking]', { loose: true })
    expect(reasoning).toBe('ab')
    expect(body).toBe('')
  })

  it('非 loose 保持原语义（仅解析开头思考块）', () => {
    const closed = splitThinking('[thinking]想一下[/thinking]回答')
    expect(closed.reasoning).toBe('想一下')
    expect(closed.body).toBe('回答')

    const open = splitThinking('[thinking]还在想')
    expect(open.reasoning).toBe('还在想')
    expect(open.body).toBe('')

    const plain = splitThinking('正文里提到 [thinking] 标签的写法')
    expect(plain.reasoning).toBe('')
    expect(plain.body).toBe('正文里提到 [thinking] 标签的写法')
  })
})

describe('countItemsAfter（删除代价提示）', () => {
  const items: ChatItem[] = [
    { kind: 'text', role: 'user', content: 'q1', id: 'u1' },
    { kind: 'text', role: 'assistant', content: 'a1', id: 'a1' },
    { kind: 'text', role: 'user', content: 'q2', id: 'u2' },
    { kind: 'text', role: 'assistant', content: 'a2', id: 'a2' },
  ]

  it('返回该消息之后的条数（不含自身）', () => {
    expect(countItemsAfter(items, 'u1')).toBe(3)
    expect(countItemsAfter(items, 'u2')).toBe(1)
  })

  it('最后一条之后为 0（代价为 0 时不打扰用户）', () => {
    expect(countItemsAfter(items, 'a2')).toBe(0)
  })

  it('找不到该消息时返回 0（按无代价处理）', () => {
    expect(countItemsAfter(items, 'missing')).toBe(0)
    expect(countItemsAfter([], 'x')).toBe(0)
  })
})

// 回归保护：引擎的 `usage` 是**每次 LLM 调用**一条（runtime.py 的 C3 通道），
// 一次提交跨多步就有多条。视图层若逐条 push，记录里会堆出 N 行"本轮用量"，
// 而状态栏只该有**一行**本回合合计 —— 且合计值必须等于各次调用之和（否则就是漏算/重复算）。
describe('mergeTurnStats（按次增量 → 本轮单行合计）', () => {
  const makeId = () => 'stats_1'

  it('多条按次增量合并进同一行，且数值等于各次之和', () => {
    const items: ChatItem[] = []
    let id = ''
    for (const d of [{ inputTokens: 100, outputTokens: 20 }, { inputTokens: 30, outputTokens: 10 }]) {
      id = mergeTurnStats(items, id, d, makeId)
    }
    expect(items).toHaveLength(1)
    const stats = items[0]
    expect(stats.kind).toBe('turn_stats')
    if (stats.kind !== 'turn_stats') throw new Error('unreachable')
    expect(stats.inputTokens).toBe(130)
    expect(stats.outputTokens).toBe(30)
  })

  it('id 为空或条目已不在时新建一行（换会话/清理后不会写进旧条目）', () => {
    const items: ChatItem[] = [{ kind: 'text', role: 'user', content: 'q', id: 'u1' }]
    const id = mergeTurnStats(items, 'gone', { inputTokens: 5, outputTokens: 1 }, () => 'stats_new')
    expect(id).toBe('stats_new')
    expect(items).toHaveLength(2)
    expect(items[1]?.id).toBe('stats_new')
  })

  it('durationSec 按累计毫秒换算，保留一位小数', () => {
    const items: ChatItem[] = []
    let id = mergeTurnStats(items, '', { inputTokens: 1, outputTokens: 1, durationMs: 1200 }, makeId)
    id = mergeTurnStats(items, id, { inputTokens: 1, outputTokens: 1, durationMs: 800 }, makeId)
    expect(id).toBe('stats_1')
    const stats = items[0]
    if (stats?.kind !== 'turn_stats') throw new Error('unreachable')
    expect(stats.durationSec).toBe(2)
  })
})
