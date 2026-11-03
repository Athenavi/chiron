import { describe, expect, it } from 'vitest'

import {
  STREAM_PREVIEW_MAX_CHARS,
  pickStreamPreview,
} from '../streamPreview'
import type { ChatItem } from '../../../components/chat/chat-types'

/**
 * 这批用例锁定的是「地图上看不到流式输出」那个回归。
 *
 * 旧实现靠 postMessage 逐 chunk 推送，改造时随 iframe 一起删掉，卡片只剩脉冲点。
 * 现在改为从宿主 `runtimes` 投影尾部文本 —— 这里的每个断言都对应那个投影的一个
 * 容易写错的选择，而不是在测 Vue。
 */

function text(content: string, extra: Partial<Record<string, unknown>> = {}): ChatItem {
  return { kind: 'text', role: 'assistant', content, id: `t-${content.slice(0, 6)}`, ...extra } as ChatItem
}

function reasoning(content: string, extra: Partial<Record<string, unknown>> = {}): ChatItem {
  return { kind: 'reasoning', content, id: `r-${content.slice(0, 6)}`, ...extra } as ChatItem
}

describe('pickStreamPreview', () => {
  it('取出正在流式输出的文字', () => {
    expect(pickStreamPreview([text('正在生成的内容', { streaming: true })])).toBe('正在生成的内容')
  })

  it('没有任何条目时返回空串，而不是抛错', () => {
    expect(pickStreamPreview([])).toBe('')
  })

  it('已落定的消息不算实时输出', () => {
    // 关键：此时卡片仍有脉冲点表示会话在跑，但不能拿一段旧文本冒充新输出
    expect(pickStreamPreview([text('上一轮的完整回答')])).toBe('')
  })

  it('用户消息即使带 streaming 也不当作模型输出', () => {
    const userTurn = { kind: 'text', role: 'user', content: '我的提问', id: 'u1', streaming: true } as ChatItem
    expect(pickStreamPreview([userTurn])).toBe('')
  })

  it('优先取最后一条 streaming 的正文', () => {
    const items = [text('旧回答', { streaming: true }), text('新回答', { streaming: true })]
    expect(pickStreamPreview(items)).toBe('新回答')
  })

  it('思考内容同样可以预览（模型先思考后作答时不该空白）', () => {
    expect(pickStreamPreview([reasoning('正在推理…', { streaming: true })])).toBe('正在推理…')
  })

  it('跳过工具调用/结果，往前找到仍在增长的正文', () => {
    // 引擎不保证正文与工具条目严格交替，工具阶段不该让预览突然清空
    const items = [
      text('我先查一下资料', { streaming: true }),
      { kind: 'tool_call', id: 'c1', name: 'search', status: 'running' } as ChatItem,
      { kind: 'tool_result', id: 'c2', content: '结果' } as ChatItem,
    ]
    expect(pickStreamPreview(items)).toBe('我先查一下资料')
  })

  it('超长内容只保留尾部，并在前面加省略号', () => {
    const long = 'A'.repeat(STREAM_PREVIEW_MAX_CHARS + 60)
    const preview = pickStreamPreview([text(long, { streaming: true })])

    // 读的人要看到「刚写出来的那句」，所以留尾不留头
    expect(preview.startsWith('…')).toBe(true)
    expect(preview).toHaveLength(STREAM_PREVIEW_MAX_CHARS + 1)
    expect(preview.endsWith('A')).toBe(true)
  })

  it('长度恰好在阈值上时不加省略号', () => {
    const exact = 'B'.repeat(STREAM_PREVIEW_MAX_CHARS)
    expect(pickStreamPreview([text(exact, { streaming: true })])).toBe(exact)
  })

  it('去掉首尾空白，避免卡片顶部空出一行', () => {
    expect(pickStreamPreview([text('\n\n  内容  \n', { streaming: true })])).toBe('内容')
  })
})
