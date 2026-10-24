/**
 * 未送达消息的本地暂存（CDS 2.0 §2.3「离线消息重试机制」的落地）。
 *
 * **为什么需要它**：发送失败时，那条消息只活在 ChatView 的内存 `items` 里；
 * 而输入框在 `emit('send')` **之前**就已经清空、草稿随之写成 null ——
 * 于是刷新一下，用户精心写的几百行上下文就没了。这正是手册点名的"崩溃吞文字"。
 *
 * 这里把「发过但没送达」的内容落盘，重载后由 Composer 顶部的横幅提供
 * 「立即重发 / 全部丢弃」（对应 CDS 的 `ChatComposerQueuedMessages`）。
 *
 * **与草稿的边界**：草稿记的是"还没发的"，outbox 记的是"发过但失败的"。
 * 两者必须分开存 —— 合并后重载会把同一条内容既当草稿又当待发消息，出现"幽灵重复"。
 *
 * 隐私：键经 `privateKey` 加账号命名空间，并登记进 `PRIVATE_KEY_PREFIXES`，
 * 换账号 / 登出时会被 `clearPrivateStorage` 一并清掉（否则下个账号能读到上一个人的提示词）。
 */
import type { ChatAttachment } from './chat-types'
import { privateKey } from '../../utils/privateStorage'

export interface OutboxEntry {
  id: string
  content: string
  attachments?: ChatAttachment[]
  /** 失败时刻（毫秒）。仅用于展示与排序，不参与重试判定。 */
  failedAt: number
  /** 已重试次数。用于"反复发不出去"时提示用户先处理网络/模型配置。 */
  retryCount: number
}

const KEY_BASE = 'chiron:outbox:v1'

function storageKey(sessionId: string, userId?: string | null): string {
  return privateKey(`${KEY_BASE}:${sessionId}`, userId)
}

/** 读某会话的待发队列。任何异常（隐私模式禁用存储 / 旧格式坏数据）都退化为空 ——
 *  存储问题**不许**挡住发送主流程。 */
export function readOutbox(sessionId: string, userId?: string | null): OutboxEntry[] {
  if (!sessionId) return []
  try {
    const raw = localStorage.getItem(storageKey(sessionId, userId))
    if (!raw) return []
    const parsed: unknown = JSON.parse(raw)
    if (!Array.isArray(parsed)) return []
    return parsed.filter(
      (e): e is OutboxEntry =>
        !!e &&
        typeof e === 'object' &&
        typeof (e as OutboxEntry).id === 'string' &&
        typeof (e as OutboxEntry).content === 'string',
    )
  } catch {
    return []
  }
}

function writeOutbox(sessionId: string, userId: string | null | undefined, entries: OutboxEntry[]): OutboxEntry[] {
  if (!sessionId) return []
  try {
    const key = storageKey(sessionId, userId)
    if (entries.length === 0) localStorage.removeItem(key)
    else localStorage.setItem(key, JSON.stringify(entries))
  } catch {
    /* 写失败不影响本次发送；用户仍能在当前页面看到失败标记 */
  }
  return entries
}

/** 记一条未送达消息（保留原附件，重发时无需重新选择）。 */
export function addToOutbox(
  sessionId: string,
  userId: string | null | undefined,
  input: { content: string; attachments?: ChatAttachment[] },
): OutboxEntry[] {
  const entries = readOutbox(sessionId, userId)
  entries.push({
    id: typeof crypto !== 'undefined' && 'randomUUID' in crypto ? crypto.randomUUID() : `o-${Date.now()}`,
    content: input.content,
    attachments: input.attachments?.length ? input.attachments : undefined,
    failedAt: Date.now(),
    retryCount: 0,
  })
  return writeOutbox(sessionId, userId, entries)
}

export function removeFromOutbox(
  sessionId: string,
  userId: string | null | undefined,
  id: string,
): OutboxEntry[] {
  return writeOutbox(sessionId, userId, readOutbox(sessionId, userId).filter(e => e.id !== id))
}

/** 重试失败时累加次数（不改变队列内容）。 */
export function bumpRetryCount(
  sessionId: string,
  userId: string | null | undefined,
  id: string,
): OutboxEntry[] {
  const entries = readOutbox(sessionId, userId).map(e =>
    e.id === id ? { ...e, retryCount: e.retryCount + 1 } : e,
  )
  return writeOutbox(sessionId, userId, entries)
}

export function clearOutbox(sessionId: string, userId?: string | null): OutboxEntry[] {
  return writeOutbox(sessionId, userId, [])
}
