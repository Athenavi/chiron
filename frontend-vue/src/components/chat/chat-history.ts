/**
 * 历史消息 → `ChatItem[]` 的**唯一**转换管线。
 *
 * ## 为什么必须共享（这是一个真实缺陷的修复）
 *
 * 这个函数原先只存在于 `ChatView.vue` 内部，于是分屏参考栏 `SessionPreviewPane`
 * 拿不到它，只能自己手写一份映射 —— 而那份映射只取了 `content`、只区分
 * user/assistant，结果是：
 *
 * - **内部格式标签直接泄露到界面**：`stripUserInputTag`（用户输入标记）与
 *   `splitThinking`（思维链）都没跑，`<thinking>` / 用户输入包装标签会原样显示；
 * - **工具调用全部丢失**：`tool_call` / `tool_result` 的 `kind` 根本没构造；
 * - 也没有日期分隔线，与主区域观感不一致。
 *
 * 所以把它抽到这里作为单一事实源：**主区域与分屏共用同一条管线**，
 * 任何关于"历史怎么变成界面条目"的规则都只在这一处。
 */
import {
  formatClock,
  splitThinking,
  stripUserInputTag,
  type ChatItem,
} from './chat-types'
import { t } from '../../i18n'

/** 元数据容错解析：字符串则尝试 JSON，失败或非对象返回 undefined。 */
export function normalizeMeta(raw: any): Record<string, any> | undefined {
  if (!raw) return undefined
  if (typeof raw === 'string') {
    try {
      raw = JSON.parse(raw)
    } catch {
      return undefined
    }
  }
  return raw && typeof raw === 'object' ? raw : undefined
}

/**
 * 把 `/v1/conversations/{id}` 返回的 `messages` + `tool_calls` 合并成时间线条目。
 *
 * 行为与原先 `ChatView` 内部版本**逐字一致**（抽取时未改动逻辑，只补了导出与注释）。
 */
export function mergeHistory(messages: any[], toolCalls: any[]): ChatItem[] {
  interface TimelineEntry {
    t: number
    items: ChatItem[]
  }
  const turnOf = (m: any): string | undefined => (m?.turn_id ? String(m.turn_id) : undefined)
  const timeline: TimelineEntry[] = (messages || [])
    .filter((m: any) => (m.role === 'user' || m.role === 'assistant') && m.content)
    .map((m: any) => {
      const clock = formatClock(m.created_at)
      const turnId = turnOf(m)
      const items: ChatItem[] = []
      if (m.role === 'user') {
        items.push({
          kind: 'text',
          role: 'user',
          content: stripUserInputTag(m.content),
          time: clock,
          id: m.id,
          turnId,
        })
      } else {
        const { reasoning, body } = splitThinking(m.content, { loose: true })
        if (reasoning) {
          items.push({ kind: 'reasoning', content: reasoning, time: clock, id: `${m.id}:r`, turnId })
        }
        if (body) {
          items.push({
            kind: 'text',
            role: 'assistant',
            content: body,
            time: clock,
            id: m.id,
            turnId,
            metadata: normalizeMeta((m as any)?.metadata),
          } as any)
        }
      }
      return { t: new Date(m.created_at).getTime(), items }
    })

  const callsById = new Map<string, any>((toolCalls || []).map((tc: any) => [tc.id, tc]))
  ;(messages || []).forEach((m: any) => {
    if (m.role !== 'assistant' || !m.tool_calls || m.tool_calls === '[]') return
    let inline: any[]
    try {
      inline = typeof m.tool_calls === 'string' ? JSON.parse(m.tool_calls) : m.tool_calls
    } catch {
      return
    }
    for (const tc of inline || []) {
      if (!tc) continue
      if (typeof tc === 'string') {
        if (!callsById.has(tc)) {
          callsById.set(tc, {
            id: tc,
            tool_name: 'tool',
            input: '',
            output: '',
            is_error: false,
            created_at: m.created_at,
            turn_id: m.turn_id,
          })
        }
        continue
      }
      if (!tc.id) continue
      const known = callsById.get(tc.id)
      if (known) {
        // 已有记录：只补回合身份，不覆盖落库的工具输出
        if (!known.turn_id) known.turn_id = m.turn_id
        continue
      }
      callsById.set(tc.id, {
        id: tc.id,
        tool_name: tc.function?.name ?? tc.name,
        input: tc.function?.arguments ?? tc.arguments ?? '',
        output: '',
        is_error: false,
        created_at: m.created_at,
        turn_id: m.turn_id,
      })
    }
  })

  Array.from(callsById.values()).forEach((tc: any) => {
    const turnId = turnOf(tc)
    const callItems: ChatItem[] = [
      {
        kind: 'tool_call',
        id: tc.id,
        name: tc.tool_name,
        arguments: tc.input || '',
        status: 'done',
        turnId,
      },
    ]
    if (tc.output) {
      callItems.push({
        kind: 'tool_result',
        toolCallId: tc.id,
        id: `${tc.id}:res`,
        content: tc.output,
        isError: !!tc.is_error,
        turnId,
      })
    }
    timeline.push({ t: new Date(tc.created_at).getTime(), items: callItems })
  })
  timeline.sort((a, b) => a.t - b.t)
  const flat = timeline.flatMap(e => e.items)
  const merged: ChatItem[] = []
  let prevDay = ''
  timeline.forEach((e, i) => {
    const d = new Date(e.t)
    const dayKey = `${d.getFullYear()}-${d.getMonth() + 1}-${d.getDate()}`
    if (i > 0 && prevDay !== dayKey) {
      merged.push({
        kind: 'date_divider',
        content: t('chat.time.monthDay', { month: d.getMonth() + 1, day: d.getDate() }),
        id: `date-${dayKey}-${i}`,
      })
    }
    merged.push(...e.items)
    prevDay = dayKey
  })
  return merged.length > flat.length ? merged : flat
}
