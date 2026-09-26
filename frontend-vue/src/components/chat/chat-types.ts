// chat 共享类型与工具函数
import dayjs from 'dayjs'
import { t } from '../../i18n'

export interface ChatSession {
  id: string
  title: string
  /** 置顶会话固定在列表顶部（登录用户存 DB，guest 随 localStorage 持久化） */
  pinned?: boolean
  /** P3-D: 会话标签（前端 localStorage 存储，用于分类筛选） */
  tag?: string
  // ── 分支血缘（后端 sessions 早已存在的列，P0 起对外暴露）──
  /** 非空表示这个会话是从另一个会话分支出来的（"分支自谁"） */
  parent_session_id?: string
  /** 父会话展示名（alias || title）；父会话已删则为空 */
  parent_title?: string
  /** 分叉点：保留到源会话的第几条消息 */
  branch_from_seq?: number
  created_at: string
  updated_at: string
}

export type ToolStatus = 'running' | 'done' | 'error'

export interface DateDividerItem extends ChatItemBase {
  kind: 'date_divider'
  content: string // 日期文本（如 8月16日）
}

export interface ChatItemBase {
  kind: string
  /** 消息时间（HH:mm 字符串）；历史消息来自 created_at，实时消息缺省（渲染时取当前时间） */
  time?: string
  /** 稳定 id（虚拟列表 key + 流式定位；历史=消息 id，实时=运行时生成） */
  id?: string
  /**
   * 所属回合（后端 turn_id）。同一回合的思考/正文/工具卡片共享一个身份域：
   * 渲染 key 与滚动锚点都用它，历史补丁与分页插入都不会让身份跨回合漂移。
   */
  turnId?: string
}

export interface TextItem extends ChatItemBase {
  kind: 'text'
  role: 'user' | 'assistant'
  content: string
  streaming?: boolean
  /** 消息发送失败（网络/服务端错误），UI 展示重试按钮 */
  error?: boolean
  /** 失败消息的错误提示文案 */
  errorMsg?: string
  /** 附件列表（用户消息可携带图片/文件，发送时一并上传） */
  attachments?: ChatAttachment[]
  /** P2-F: 生成被用户手动停止，显示"继续生成"提示 */
  stopped?: boolean
  /**
   * 消息来源（后端 messages.source）：'subagent_followup' = 子 Agent 自动轮注入的消息，
   * 渲染成系统提示而不是用户气泡（见 internal/api/agent_followup.go）。
   */
  source?: string
  /**
   * 引擎侧附加信息（知识库引用 / 工作流来源 / trace_id 等），驱动 `MessageItem` 的
   * 反向定位 chips。仅部分链路有：python-engine 的 unified_executor 会带
   * （app/api/unified_executor.py:416），Go 网关的 conversation.Message 没有该字段。
   */
  metadata?: Record<string, unknown>
}

export interface ChatAttachment {
  id: string
  name: string
  size: number
  mimeType: string
  /** 上传后的资源 URL（如 /v1/media/{id}/download 或 data: URL 预览） */
  url: string
  /** 是否为图片（图片在消息气泡内内联展示） */
  isImage: boolean
  /** 是否为音频（音频在消息气泡内显示播放器） */
  isAudio?: boolean
}

export interface ReasoningItem extends ChatItemBase {
  kind: 'reasoning'
  content: string
  streaming?: boolean
}

export interface ToolCallItem extends ChatItemBase {
  kind: 'tool_call'
  id: string
  name: string
  arguments: string
  status: ToolStatus
}

export interface ToolResultItem extends ChatItemBase {
  kind: 'tool_result'
  toolCallId: string
  content: string
  isError: boolean
}

export interface TurnStatsItem extends ChatItemBase {
  kind: 'turn_stats'
  inputTokens: number
  outputTokens: number
  durationSec?: number
}

export type ChatItem = TextItem | ReasoningItem | ToolCallItem | ToolResultItem | TurnStatsItem | DateDividerItem

/**
 * assistant 消息上内联的 tool_call（OpenAI 形状）。
 * `id` 之外全部可选：字符串化 JSON 里缺字段是常态，解析失败由调用方跳过。
 */
export interface InlineToolCall {
  id?: string
  name?: string
  /** OpenAI 的 arguments 是 JSON **字符串**，不是对象 */
  arguments?: string
  function?: { name?: string; arguments?: string }
}

/**
 * `/v1/conversations/{id}` 的 `messages[]` 形状。
 *
 * 依据 `internal/api/conversation.go` 的 `Message`，并补上 python-engine 链路额外携带的
 * `metadata` / `error`（`app/api/unified_executor.py:839`）。字段一律可选：历史来自两个
 * 后端，形状并不完全一致，缺失时走各自字段的兜底。
 */
export interface HistoryMessage {
  id?: string
  role?: string
  content?: string
  /** OpenAI 格式 tool_calls —— 落库为 JSON 字符串，部分路径已是数组 */
  tool_calls?: string | InlineToolCall[] | null
  turn_id?: string
  created_at?: string
  metadata?: unknown
  error?: unknown
}

/**
 * `tool_calls[]` 的形状（`internal/api/conversation.go` 的 `ToolCall`）。
 * `turn_id` 不在后端契约里 —— 由所属消息的内联 tool_calls 回填（见 `mergeHistory`）。
 */
export interface HistoryToolCall {
  id: string
  tool_name?: string
  input?: string
  output?: string
  is_error?: boolean
  created_at?: string
  turn_id?: string
}

export const THINK_START = '[thinking]'
export const THINK_END = '[/thinking]'

/** 格式化文件大小 */
export function formatSize(bytes: number): string {
  if (!bytes) return ''
  const k = 1024
  const sizes = ['B', 'KB', 'MB', 'GB']
  const i = Math.floor(Math.log(bytes) / Math.log(k))
  return parseFloat((bytes / Math.pow(k, i)).toFixed(1)) + ' ' + sizes[i]
}

/**
 * 相对时间（刚刚 / N 分钟前 / N 小时前 / 日期）。
 *
 * translate 由调用方注入：组件内应传 useI18n() 的 t（响应式，切换语言即时更新），
 * 默认回退到全局 t（非响应式，只适合一次性字符串）。
 * 日期不再硬编码 'zh-CN' —— dayjs 的 locale 由 setLocale() 同步，随界面语言变化。
 */
export function formatRelativeTime(
  iso: string,
  translate: (key: string, named?: Record<string, unknown>) => string = t,
): string {
  if (!iso) return ''
  const d = new Date(iso)
  const now = new Date()
  const diff = now.getTime() - d.getTime()
  if (diff < 60000) return translate('chat.time.justNow')
  if (diff < 3600000) return translate('chat.time.minutesAgo', { n: Math.floor(diff / 60000) })
  if (diff < 86400000) return translate('chat.time.hoursAgo', { n: Math.floor(diff / 3600000) })
  return dayjs(d).format('MMM D')
}

/** 时钟时间（HH:mm）：历史消息日期还原（S 修复：不再永远显示当前时间） */
export function formatClock(iso?: string): string {
  if (!iso) return ''
  const d = new Date(iso)
  if (Number.isNaN(d.getTime())) return ''
  return `${d.getHours().toString().padStart(2, '0')}:${d.getMinutes().toString().padStart(2, '0')}`
}

/** 解析流式内容中的思考块与正文（deepseek ReasoningRow 语义）
 *
 * 语义依据：main.py 的思考模式 prompt 要求模型"先输出 [thinking]...[/thinking]
 * 再输出回答"，故思考块只解析**消息开头**位置：
 * 1. 开头完整闭合 `[thinking]...[/thinking]` → 提取为 reasoning，其余为 body
 * 2. 开头未闭合 `[thinking]...`（流式进行中）→ 全部为 reasoning
 * 3. 其它位置出现 [thinking] 字样（讲解/代码示例）→ 完整保留在正文，绝不吞内容
 *
 * loose=true（历史回放 / 流式实时解析）：引擎按 ~80 字一段下发
 * `[thinking]片段[/thinking]`（python-engine/app/agent/runtime.py），因此文本里会出现
 * 多段连续思考块，流式时末段还可能尚未闭合。此时用状态机整体扫描：
 * 进入 [thinking] 后的文本归 reasoning，遇到 [/thinking] 回到正文；未闭合的尾段仍算
 * reasoning（它还在思考），避免 `[/thinking][thinking]` 这类标签残留到正文气泡。
 */
export function splitThinking(src: string, opts?: { loose?: boolean }): { reasoning: string; body: string } {
  if (opts?.loose) {
    const reasoningParts: string[] = []
    const bodyParts: string[] = []
    let inThinking = false
    for (const token of src.split(/(\[thinking\]|\[\/thinking\])/)) {
      if (token === THINK_START) { inThinking = true; continue }
      if (token === THINK_END) { inThinking = false; continue }
      if (!token) continue
      if (inThinking) reasoningParts.push(token)
      else bodyParts.push(token)
    }
    // 多段是同一段思考被 chunk 切割的结果，直接拼接还原原文
    return { reasoning: reasoningParts.join('').trim(), body: bodyParts.join('').trim() }
  }
  const closed = src.match(/^\s*\[thinking\]([\s\S]*?)\[\/thinking\]([\s\S]*)$/)
  if (closed) {
    return { reasoning: closed[1].trim(), body: closed[2].trim() }
  }
  const tail = src.match(/^\s*\[thinking\]([\s\S]*)$/)
  if (tail) {
    return { reasoning: tail[1].trim(), body: '' }
  }
  return { reasoning: '', body: src.trim() }
}

/** 剥离后端安全净化添加的 <user_input> 包装（Go InputSanitizer.Sanitize） */
export function stripUserInputTag(content: string): string {
  return content.replace(/^\s*<user_input>\s*([\s\S]*?)\s*<\/user_input>\s*$/, '$1').trim()
}

/**
 * rAF 节流（deepseek use-throttled-visual-update）。
 *
 * `never[]` 而不是 `any[]`：函数参数位置是逆变的，用 `unknown[]` 会让任何具体签名的
 * 函数都赋不进来，`never[]` 才是「接受任意参数列表」的正确写法。
 */
export function throttleRaf<T extends (...args: never[]) => void>(fn: T): T {
  const call = fn as (...args: Parameters<T>) => void
  let raf = 0
  let lastArgs: Parameters<T> | null = null
  const wrapped = ((...args: Parameters<T>) => {
    lastArgs = args
    if (raf) return
    raf = requestAnimationFrame(() => {
      raf = 0
      const pending = lastArgs
      lastArgs = null
      if (pending) call(...pending)
    })
  }) as T
  return wrapped
}

/**
 * 指定消息之后还有多少条（不含它自己）。
 *
 * 用于删除类操作（重发 / 重新生成 / 重试）前的代价提示：这些操作会截断到该消息，
 * 后端没有回滚接口，所以必须让用户在动手前知道会丢几条。找不到该消息时返回 0。
 */
export function countItemsAfter(items: readonly ChatItem[], itemId: string): number {
  const index = items.findIndex(item => item.id === itemId)
  return index < 0 ? 0 : items.length - index - 1
}
