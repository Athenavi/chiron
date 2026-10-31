/**
 * 失败消息标记与手动修复，从 `ChatView.vue` 抽出。
 *
 * 边界：只负责把"这次失败是什么性质"写进那条消息本身，以及收口卡住的会话。
 * 不发起对话、不改流状态 —— `items` 与 `activeSessionId` 由调用方注入。
 *
 * 为什么要记 `errorCode` / `retryable` / `reason`：错误块据此给出**针对性动作**。
 * `session_busy` 该显示"修复会话 + 约 N 分钟自动恢复"，而不是"重试"
 * （重试必然再撞同一道闸门）；服务端说"重试没意义"时，UI 就不该给重试按钮。
 */
import type { Ref } from 'vue'
import { useI18n } from 'vue-i18n'

import { recoverSession } from '../api'
import type { ChatItem } from '../components/chat/chat-types'
import { describeApiError } from '../utils/apiError'
import { notify } from '../utils/notify'

export interface MessageFailureDeps {
  /** 当前会话的消息列表 */
  items: Ref<ChatItem[]>
  /** 当前会话 id（修复会话时用） */
  activeSessionId: Ref<string>
}

export interface FailureMeta {
  code?: string
  retryAfterSec?: number
  retryable?: boolean
  reason?: string
}

export function useMessageFailure(deps: MessageFailureDeps) {
  const { t } = useI18n()
  const { items, activeSessionId } = deps

  function markMessageFailed(
    itemId: string | undefined,
    errorMsg: string,
    meta?: FailureMeta,
  ) {
    if (!itemId) return
    const it = items.value.find(i => i.id === itemId)
    if (it && it.kind === 'text') {
      it.error = true
      it.errorMsg = errorMsg
      // 记下后端错误码与"最坏等待秒数"：错误块据此给出**针对性动作**
      it.errorCode = meta?.code
      it.errorRetryAfterSec = meta?.retryAfterSec
      // 服务端说"重试没意义"时，UI 就不该给重试按钮（见 MessageItem 的渲染条件）。
      it.errorRetryable = meta?.retryable
      // 模型被拒时区分"谁拦的"，据此决定给"买套餐"还是"找管理员"。
      it.errorReason = meta?.reason
    }
  }

  /**
   * 从失败响应里取"结构性"信息：稳定错误码 + 自动恢复的最坏等待秒数。
   *
   * 秒数由后端算（stale 阈值 + 一次巡检间隔），前端只负责呈现 ——
   * 这样"还要等多久"与实际自动恢复时机不会各说各话。
   */
  function describeFailureMeta(e: unknown): FailureMeta {
    const data = (
      e as {
        response?: {
          data?: {
            code?: string
            retryable?: boolean
            params?: { retry_after_seconds?: number; reason?: string }
          }
        }
      }
    )?.response?.data
    if (!data) return {}
    const sec = data.params?.retry_after_seconds
    const reason = data.params?.reason
    return {
      code: typeof data.code === 'string' ? data.code : undefined,
      retryAfterSec: typeof sec === 'number' ? sec : undefined,
      // 只在服务端**显式**给出布尔时才传：缺省表示"无结论"，调用方应保持原行为
      // （见 rateLimitBackoff.shouldRetryResponse 的同一条口径）。
      retryable: typeof data.retryable === 'boolean' ? data.retryable : undefined,
      reason: typeof reason === 'string' ? reason : undefined,
    }
  }

  /**
   * 手动修复卡住的会话：把同会话那条一直没收口的 run 收口。
   *
   * 网关是**严格模式**：仍在跑（Redis 运行锁在）或没到 stale 窗口时会回 409，
   * 并把"还要等多久"放在 params 里 —— 这里如实转告，而不是笼统报"失败"。
   */
  async function repairSession(itemId: string) {
    const target = activeSessionId.value
    if (!target) return
    try {
      await recoverSession(target)
      const it = items.value.find(i => i.id === itemId)
      if (it && it.kind === 'text') {
        // 收口成功、或本来就没卡（已被自动收口）—— 两种都是"现在可以发了"
        it.error = false
        it.errorMsg = undefined
        it.errorCode = undefined
        it.errorRetryAfterSec = undefined
      }
      notify.success(t('errors.session_repaired'))
    } catch (e) {
      const meta = describeFailureMeta(e)
      const min = meta.retryAfterSec ? Math.max(1, Math.ceil(meta.retryAfterSec / 60)) : 0
      if (min) {
        // 409：确实还在跑 / 还没到 stale 窗口 —— 告诉用户具体还要等多久
        notify.warning(`${t('errors.session_busy')} ${t('errors.session_busy_auto_recover', { n: min })}`)
      } else {
        notify.error(describeApiError(e))
      }
    }
  }

  return { markMessageFailed, describeFailureMeta, repairSession }
}
