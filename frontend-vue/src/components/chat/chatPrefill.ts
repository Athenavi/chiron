import { t } from '../../i18n'

/**
 * 「在对话中继续」的跨页面投递。
 *
 * Agent 会话 / 工作流执行完的结果，用户往往想接着讨论 —— 但结果动辄几千字，
 * 塞进 URL query 既超长又难看。这里把要讨论的内容暂存到 sessionStorage，
 * 对话页取**一次**即删（不重复插入），并且只在当前标签页可见。
 */

export const CHAT_PREFILL_KEY = 'chiron:chat-prefill:v1'

/** 投递内容上限：超出则截断并标注，避免把整篇结果倒进输入框 */
export const PREFILL_MAX_CHARS = 4000

export interface ChatPrefill {
  /** 结果来源标题（如「运行结果 · 研究助手」） */
  title: string
  /** 结果正文 */
  text: string
  /** 来源页面标识，仅用于提示语（chat / agent / workflow / capability） */
  source?: string
  /**
   * 投递意图。缺省 ``result``：把上一轮产出的内容带回来继续讨论。
   * ``capability``：把「我要用这条能力」带进对话（能力发现页用）。
   */
  kind?: 'result' | 'capability'
}

/** 截断提示：用函数而非常量 —— 常量在模块加载时求值一次，语言切换后会停在旧语言。 */
function truncatedNote(): string {
  return t('common.content_too_long_truncated')
}

/** 组装要插入输入框的文本（纯函数：格式与截断都可测） */
export function buildPrefillText(prefill: ChatPrefill, maxChars = PREFILL_MAX_CHARS): string {
  // 两种投递意图的开场白完全不同：把能力当「结果」投递会生成
  // 「以下是『Execute Python Code』的结果」这种自相矛盾的话。
  const header = prefill.kind === 'capability'
    ? t('我想用「{title}」来做：', { title: prefill.title })
    : prefill.title
      ? t('以下是「{title}」的结果，请基于它继续：', { title: prefill.title })
      : t('common.below_are_the_previous_round_s_results_continue_based_on_them')
  const body = (prefill.text || '').trim()
  if (!body) return header
  if (maxChars > 0 && body.length > maxChars) {
    return `${header}\n\n${body.slice(0, maxChars)}\n\n${truncatedNote()}`
  }
  return `${header}\n\n${body}`
}

function storage(): Storage | null {
  try {
    return typeof sessionStorage === 'undefined' ? null : sessionStorage
  } catch {
    return null
  }
}

export function setChatPrefill(prefill: ChatPrefill): void {
  const store = storage()
  if (!store) return
  try {
    store.setItem(CHAT_PREFILL_KEY, JSON.stringify(prefill))
  } catch {
    // 隐私模式 / 配额不足：静默降级（用户仍可手动复制结果）
  }
}

/** 读取并清除：一次性消费，避免每次进入对话都重复插入 */
export function takeChatPrefill(): ChatPrefill | null {
  const store = storage()
  if (!store) return null
  try {
    const raw = store.getItem(CHAT_PREFILL_KEY)
    store.removeItem(CHAT_PREFILL_KEY)
    if (!raw) return null
    const parsed = JSON.parse(raw) as Partial<ChatPrefill> | null
    if (!parsed || typeof parsed.text !== 'string') return null
    return {
      title: typeof parsed.title === 'string' ? parsed.title : '',
      text: parsed.text,
      source: typeof parsed.source === 'string' ? parsed.source : undefined,
      kind: parsed.kind === 'capability' ? 'capability' : 'result',
    }
  } catch {
    return null
  }
}
