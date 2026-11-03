/**
 * 会话地图卡片的流式预览。
 *
 * 旧实现是 iframe：宿主用 `forwardStreamToMap` 把每个 SSE chunk 通过 postMessage
 * 推进画布。改造成同 SPA 视图时，那条通道随 iframe 一起被删除，而新地图只接管了
 * `runningSessionIds`（一个布尔集合）—— 于是卡片上只剩「运行中」脉冲点，看不到
 * 任何正在生成的文字。
 *
 * 流式内容本来就按会话存在宿主的 `runtimes` 里（`SessionRunState.items`），
 * 这里只是把它投影成卡片上的一行预览，**不新建第二份缓冲区** —— 两处内容一旦
 * 各自累积，就必然漂移，而漂移后的表现是「地图和对话页显示的像是两个模型在说话」。
 */

import type { ChatItem } from '../../components/chat/chat-types'

/**
 * 预览保留的字符数。
 *
 * 卡片是 220px 宽、两行高的缩略视图，200 来个字符已足够填满；再多只是白白让
 * 每个 chunk 的 diff 变大。完整内容在详情面板与对话页。
 */
export const STREAM_PREVIEW_MAX_CHARS = 240

/**
 * 取出某个会话「当前正在流式输出的那一段」的尾部。
 *
 * 三个刻意的选择：
 *
 * 1. **取尾部不取头部**：用户要看的是「刚写出来的那句」，开头几个字早读过了。
 * 2. **只认 `streaming` 为真的条目**：已落定的消息不构成实时反馈。此时返回空串
 *    （卡片仍有脉冲点表明会话在跑），好过让一段旧文本冒充新输出。
 * 3. **跳过 `tool_call` / `tool_result`**：那一阶段没有可展示的正文，`continue`
 *    往前找 —— 工具调用之前可能已有一条仍在增长的文本（引擎不保证两类条目交替
 *    严格成对）。
 */
export function pickStreamPreview(items: readonly ChatItem[]): string {
  for (let i = items.length - 1; i >= 0; i -= 1) {
    const item = items[i]
    if (!item) continue
    if (item.kind === 'text') {
      // 用户消息不会处于 streaming 态；即便有，也不是「模型正在输出」
      if (item.role !== 'assistant' || !item.streaming) continue
      return tail(item.content)
    }
    if (item.kind === 'reasoning') {
      if (!item.streaming) continue
      return tail(item.content)
    }
  }
  return ''
}

function tail(text: string): string {
  const trimmed = text.trim()
  if (trimmed.length <= STREAM_PREVIEW_MAX_CHARS) return trimmed
  // 省略号放前面：读的人立刻知道「这前面还有」，而不是以为输出只有这么点
  return `…${trimmed.slice(-STREAM_PREVIEW_MAX_CHARS)}`
}
