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
  type HistoryMessage,
  type HistoryToolCall,
  type InlineToolCall,
} from './chat-types'
import { t } from '../../i18n'

/**
 * 元数据容错解析：字符串则尝试 JSON，失败或非对象返回 undefined。
 *
 * 入参是 `unknown` 而非 `any`：后端两条链路（Go 网关 / Python 引擎）给的形状不一致，
 * 这里是边界，必须显式窄化而不是把 `any` 透给调用方。
 */
export function normalizeMeta(raw: unknown): Record<string, unknown> | undefined {
  if (!raw) return undefined
  let value: unknown = raw
  if (typeof value === 'string') {
    try {
      value = JSON.parse(value)
    } catch {
      return undefined
    }
  }
  if (!value || typeof value !== 'object' || Array.isArray(value)) return undefined
  return value as Record<string, unknown>
}

/**
 * 把 `/v1/conversations/{id}` 返回的 `messages` + `tool_calls` 合并成时间线条目。
 *
 * 行为与原先 `ChatView` 内部版本**逐字一致**（抽取时未改动逻辑，只补了导出与注释）。
 */
export function mergeHistory(
  messages: readonly HistoryMessage[] | null | undefined,
  toolCalls: readonly HistoryToolCall[] | null | undefined,
): ChatItem[] {
  interface TimelineEntry {
    t: number
    items: ChatItem[]
  }
  const turnOf = (m: { turn_id?: string }): string | undefined =>
    m?.turn_id ? String(m.turn_id) : undefined
  const timeline: TimelineEntry[] = (messages || [])
    .filter(m => (m.role === 'user' || m.role === 'assistant') && m.content)
    .map(m => {
      const content = m.content || ''
      const clock = formatClock(m.created_at)
      const turnId = turnOf(m)
      const items: ChatItem[] = []
      if (m.role === 'user') {
        items.push({
          kind: 'text',
          role: 'user',
          content: stripUserInputTag(content),
          time: clock,
          id: m.id,
          turnId,
        })
      } else {
        const { reasoning, body } = splitThinking(content, { loose: true })
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
            metadata: normalizeMeta(m.metadata),
          })
        }
      }
      // created_at 缺失时 new Date('') 与原 new Date(undefined) 同为 Invalid Date（NaN），
      // 排序表现不变
      return { t: new Date(m.created_at || '').getTime(), items }
    })

  const callsById = new Map<string, HistoryToolCall>(
    (toolCalls || []).map(call => [call.id, call]),
  )
  for (const m of messages || []) {
    if (m.role !== 'assistant' || !m.tool_calls || m.tool_calls === '[]') continue
    let inline: unknown
    if (typeof m.tool_calls === 'string') {
      try {
        inline = JSON.parse(m.tool_calls)
      } catch {
        continue
      }
    } else {
      inline = m.tool_calls
    }
    for (const raw of Array.isArray(inline) ? inline : []) {
      if (!raw) continue
      // 内联项可能是纯 id 字符串（只引用落库记录）
      if (typeof raw === 'string') {
        if (!callsById.has(raw)) {
          callsById.set(raw, {
            id: raw,
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
      const tc = raw as InlineToolCall
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
  }

  for (const tc of callsById.values()) {
    const turnId = turnOf(tc)
    const callItems: ChatItem[] = [
      {
        kind: 'tool_call',
        id: tc.id,
        name: tc.tool_name || '',
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
    timeline.push({ t: new Date(tc.created_at || '').getTime(), items: callItems })
  }
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
