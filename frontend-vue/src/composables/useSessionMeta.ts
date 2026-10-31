/**
 * 会话元数据操作（重命名 / 置顶 / 标签 / 排序），从 `ChatView.vue` 抽出。
 *
 * 边界：只改 `sessions` 数组里的**元数据**，不碰消息流、不切会话、不发请求以外的东西。
 * 会话列表本身与本地缓存由调用方持有（`sessions` / `persistSessions` 依赖注入），
 * 因此本 composable 不拥有任何会话状态，也就不参与 `activeSessionId` 的生命周期。
 *
 * 共同模式：**先改本地、再落库、失败回滚**。这样点置顶/改标签的反馈是即时的，
 * 而服务端失败时界面不会停在"看起来成功了"的假状态上。
 */
import { ref } from 'vue'
import type { Ref } from 'vue'
import { useI18n } from 'vue-i18n'

import { updateConversation } from '../api'
import type { ChatSession } from '../components/chat/chat-types'
import { errorDetail } from '../utils/apiError'
import { notify } from '../utils/notify'

export interface SessionMetaDeps {
  /** 会话列表（调用方持有，本 composable 只就地改元数据） */
  sessions: Ref<ChatSession[]>
  /** 把 `sessions` 写回本地缓存 */
  persistSessions: () => void
}

export function useSessionMeta(deps: SessionMetaDeps) {
  const { t } = useI18n()
  const { sessions, persistSessions } = deps

  // ── 置顶（列表排序：pinned DESC + updated_at DESC） ──
  function sortSessions() {
    sessions.value = [...sessions.value].sort((a, b) => {
      if (!!a.pinned !== !!b.pinned) return a.pinned ? -1 : 1
      return new Date(b.updated_at || 0).getTime() - new Date(a.updated_at || 0).getTime()
    })
  }

  async function togglePin(id: string, pinned: boolean) {
    const s = sessions.value.find(x => x.id === id)
    if (!s) return
    const prev = s.pinned
    s.pinned = pinned
    sortSessions()
    persistSessions()
    try {
      await updateConversation(id, { pinned })
    } catch {
      s.pinned = prev
      sortSessions()
      persistSessions()
      notify.error(t('errors.pin_failed'))
    }
  }

  // 设置会话标签（DB 持久化：写 sessions.tag；失败回滚本地状态）
  // 注意：旧实现只写 localStorage，而 loadSessions 每次都用 API 数据覆盖 sessions 数组，
  // 刷新后标签必然丢失 —— 故必须落库。
  async function setSessionTag(id: string, tag: string) {
    const s = sessions.value.find(x => x.id === id)
    if (!s) return
    const prev = s.tag
    s.tag = tag || undefined
    sortSessions()
    persistSessions()
    try {
      // 空串表示清除标签（后端 NULLIF 写 NULL）
      await updateConversation(id, { tag: tag || '' })
      notify.success(tag ? t('settings.tag_set_tag', { tag }) : t('common.tags_cleared'))
    } catch {
      s.tag = prev
      sortSessions()
      persistSessions()
      notify.error(t('errors.failed_to_save_tag'))
    }
  }

  // ── 重命名（deepseek session rename dialog：Modal + 行内输入框） ──
  const renameTarget = ref<ChatSession | null>(null)
  const renameDraft = ref('')
  const renaming = ref(false)

  function openRename(id: string, currentTitle: string) {
    const s = sessions.value.find(x => x.id === id)
    if (!s) return
    renameTarget.value = s
    renameDraft.value = currentTitle
  }

  async function confirmRename() {
    const target = renameTarget.value
    const title = renameDraft.value.trim()
    if (!title || !target) return
    renaming.value = true
    try {
      await updateConversation(target.id, { title })
      const s = sessions.value.find(x => x.id === target.id)
      if (s) s.title = title
      persistSessions()
      notify.success(t('common.renamed'))
      renameTarget.value = null
    } catch (e) {
      notify.error(t('errors.rename_failed') + errorDetail(e, t('errors.network_error_2')))
    } finally {
      renaming.value = false
    }
  }

  return {
    sortSessions,
    togglePin,
    setSessionTag,
    renameTarget,
    renameDraft,
    renaming,
    openRename,
    confirmRename,
  }
}
