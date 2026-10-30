import { ref, computed } from 'vue'
import { message } from 'ant-design-vue'
import { useI18n } from 'vue-i18n'
import { api } from '../api'
import { serverErrorMessage } from '../utils/apiError'
import { tallyCache, scheduleTally } from '../utils/tallyCache'

export interface ConversationItem {
  id: string
  title?: string
  updated_at?: string
  created_at?: string
}

/** 导出用的消息行（`/v1/conversations/{id}` 的 messages[]）；字段都可能缺，渲染时逐项兜底 */
export interface ExportMessage {
  role?: string
  content?: unknown
  reasoning?: unknown
}

export interface UseSettingsConversationsOptions {
  /**
   * 会话删除后要一起做的事。
   *
   * 删会话会让**分享列表**跟着失效（分享挂在会话上，会话没了分享自然不该再显示），
   * 所以视图把 `useSettingsShares` 的 `loadShares` 注进来。不直接 import 分享 composable
   * 是刻意的：两个子域各自独立可测，谁也不需要知道对方内部怎么实现。
   */
  onSessionsDeleted?: () => Promise<void> | void
}

/**
 * 设置面板的「数据管理：会话导出与删除」子域。
 *
 * 从 `SettingsPanel.vue` 抽出，逻辑逐字未改。与另外两个数据块同构，两套入口不可合并：
 * `openConversations` 延后+命中缓存，`loadConversations` 强制绕过（删完还显示旧列表最坏）。
 */
export function useSettingsConversations(options: UseSettingsConversationsOptions = {}) {
  const { t } = useI18n()

  const conversations = ref<ConversationItem[]>([])
  const convLoading = ref(false)
  const selectedConvIds = ref<string[]>([])
  const exporting = ref(false)
  const deleting = ref(false)

  async function fetchConversations(): Promise<ConversationItem[]> {
    const { data } = await api.get('/v1/conversations')
    // 兼容 {items:[...]} 与直接数组两种返回形态
    return data?.data?.items ?? data?.data ?? []
  }

  /** 数据变更后 / 手动刷新：绕过缓存（删完会话还显示旧列表才是最坏的） */
  async function loadConversations() {
    convLoading.value = true
    try {
      conversations.value = await tallyCache.load('conversations', fetchConversations, { force: true })
    } catch (e) {
      message.error(serverErrorMessage(e, t('errors.failed_to_load_session_list')))
    } finally {
      convLoading.value = false
    }
  }

  /** 面板打开时用：延后 + 命中缓存即渲染 */
  function openConversations() {
    return scheduleTally(
      tallyCache,
      'conversations',
      fetchConversations,
      (items) => {
        conversations.value = items
        convLoading.value = false
      },
      {
        onStart: () => { convLoading.value = true },
        onError: (e) => {
          convLoading.value = false
          message.error(serverErrorMessage(e, t('errors.failed_to_load_session_list')))
        },
      },
    )
  }

  /** 把一次会话渲染成 Markdown；reasoning 单独成节，避免与正文混淆 */
  function conversationToMarkdown(conv: ConversationItem, messages: ExportMessage[]): string {
    const lines: string[] = [
      t('common.title', { title: conv.title || t('chat.untitled_session') }),
      '',
      t('common.exported_at_time', { time: new Date().toLocaleString() }),
      '',
    ]
    for (const m of messages) {
      const role = m.role === 'user' ? t('admin.user_2') : m.role === 'assistant' ? t('common.assistant_2') : (m.role || t('common.unknown'))
      lines.push(`## ${role}`, '')
      if (m.reasoning) {
        lines.push('<details><summary>' + t('chat.reasoning_process') + '</summary>', '', String(m.reasoning), '', '</details>', '')
      }
      lines.push(String(m.content ?? ''), '')
    }
    return lines.join('\n')
  }

  /** 逐会话拉详情再打包。导出是低频操作，串行可避免瞬时打满后端。 */
  async function exportSelected() {
    const ids = selectedConvIds.value
    if (!ids.length) {
      message.warning(t('chat.please_select_the_session_to_export_first'))
      return
    }
    exporting.value = true
    try {
      const parts: string[] = []
      for (const id of ids) {
        const conv = conversations.value.find(c => c.id === id) ?? { id }
        const { data } = await api.get(`/v1/conversations/${id}`)
        const payload = data?.data ?? {}
        const messages = payload.messages ?? payload.items ?? []
        parts.push(conversationToMarkdown(conv, messages), '\n---\n')
      }
      const blob = new Blob([parts.join('\n')], { type: 'text/markdown;charset=utf-8' })
      const url = URL.createObjectURL(blob)
      const a = document.createElement('a')
      a.href = url
      a.download = `chiron-conversations-${new Date().toISOString().slice(0, 10)}.md`
      a.click()
      URL.revokeObjectURL(url)
      message.success(t('chat.exported_n_sessions', { n: ids.length }))
    } catch (e) {
      message.error(serverErrorMessage(e, t('errors.export_failed')))
    } finally {
      exporting.value = false
    }
  }

  async function deleteSelected() {
    const ids = selectedConvIds.value
    if (!ids.length) {
      message.warning(t('chat.please_select_the_session_to_delete_first'))
      return
    }
    deleting.value = true
    try {
      // 后端无批量接口，逐个删除；部分失败时如实报告成功数，不谎报全部成功
      let ok = 0
      const failed: string[] = []
      for (const id of ids) {
        try {
          await api.delete(`/v1/conversations/${id}`)
          ok++
        } catch {
          failed.push(id)
        }
      }
      if (failed.length) {
        message.warning(t('errors.deleted_ok_failed_failed', { ok, failed: failed.length }))
      } else {
        message.success(t('chat.deleted_n_sessions', { n: ok }))
      }
      selectedConvIds.value = failed
      await loadConversations()
      await options.onSessionsDeleted?.()
    } finally {
      deleting.value = false
    }
  }

  const allSelected = computed(() =>
    conversations.value.length > 0 && selectedConvIds.value.length === conversations.value.length)

  function toggleSelectAll() {
    selectedConvIds.value = allSelected.value ? [] : conversations.value.map(c => c.id)
  }

  const selectedCount = computed(() => selectedConvIds.value.length)

  return {
    conversations,
    convLoading,
    selectedConvIds,
    exporting,
    deleting,
    allSelected,
    selectedCount,
    loadConversations,
    openConversations,
    conversationToMarkdown,
    exportSelected,
    deleteSelected,
    toggleSelectAll,
  }
}
