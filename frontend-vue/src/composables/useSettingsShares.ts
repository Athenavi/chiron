import { ref } from 'vue'
import { message } from 'ant-design-vue'
import { useI18n } from 'vue-i18n'
import { api } from '../api'
import { serverErrorMessage } from '../utils/apiError'
import { tallyCache, scheduleTally } from '../utils/tallyCache'

export interface ShareItem {
  share_id: string
  session_id: string
  title: string
  message_count: number
  created_at: string
}

/**
 * 设置面板的「数据管理：分享」子域。
 *
 * 从 `SettingsPanel.vue` 抽出，逻辑逐字未改。与三方绑定同构，两套入口不能合并：
 * - `openShares()` —— 面板打开时用，延后 + 命中缓存即渲染；
 * - `loadShares()` —— 数据变更后（撤销分享 / 删会话）用，`force` 绕缓存重取。
 *
 * 之所以值得缓存：`GET /v1/shares` 要按 `user_id` 扫 `conversation_shares` 全表
 * （基线只有主键索引），再对每行做 `array_length` 聚合算消息数。而用户多半只是路过设置页。
 */
export function useSettingsShares() {
  const { t } = useI18n()

  const shares = ref<ShareItem[]>([])
  const sharesLoading = ref(false)
  const revoking = ref('')

  async function fetchShares(): Promise<ShareItem[]> {
    const { data } = await api.get('/v1/shares')
    return data?.data?.items ?? []
  }

  /** 数据变更后 / 手动刷新：绕过缓存重取（force）并把新值写回缓存 */
  async function loadShares() {
    sharesLoading.value = true
    try {
      shares.value = await tallyCache.load('shares', fetchShares, { force: true })
    } catch (e) {
      message.error(serverErrorMessage(e, t('errors.failed_to_load_shares')))
    } finally {
      sharesLoading.value = false
    }
  }

  /** 面板打开时用：延后 + 命中缓存即渲染 */
  function openShares() {
    return scheduleTally(
      tallyCache,
      'shares',
      fetchShares,
      (items) => {
        shares.value = items
        sharesLoading.value = false
      },
      {
        onStart: () => { sharesLoading.value = true },
        onError: (e) => {
          sharesLoading.value = false
          message.error(serverErrorMessage(e, t('errors.failed_to_load_shares')))
        },
      },
    )
  }

  async function handleRevokeShare(item: ShareItem) {
    revoking.value = item.share_id
    try {
      // 撤销是按会话维度的（后端会把该会话所有有效分享标记 revoked）
      await api.delete(`/v1/conversations/${item.session_id}/share`)
      message.success(t('common.sharing_canceled'))
      await loadShares()
    } catch (e) {
      message.error(serverErrorMessage(e, t('errors.failed_to_cancel_sharing')))
    } finally {
      revoking.value = ''
    }
  }

  function shareURL(id: string) {
    return `${window.location.origin}/share/${id}`
  }

  async function copyShareURL(id: string) {
    try {
      await navigator.clipboard.writeText(shareURL(id))
      message.success(t('common.share_link_copied'))
    } catch {
      message.warning(t('errors.copy_failed_please_copy_manually'))
    }
  }

  return {
    shares,
    sharesLoading,
    revoking,
    loadShares,
    openShares,
    handleRevokeShare,
    shareURL,
    copyShareURL,
  }
}
