import { ref } from 'vue'
import { message } from 'ant-design-vue'
import { useI18n } from 'vue-i18n'
import {
  listIdentities,
  deleteIdentity,
  listPublicSsoProviders,
  ssoLoginURL,
  type UserIdentity,
  type SsoPublicProvider,
} from '../api/auth'
import { serverErrorMessage } from '../utils/apiError'
import { tallyCache, scheduleTally } from '../utils/tallyCache'

/**
 * 设置面板的「三方账号绑定」子域。
 *
 * 从 `SettingsPanel.vue` 抽出，逻辑逐字未改。缓存策略是本子域的核心复杂度，
 * 抽出来时原样保留两套入口：
 * - `openBindings()` —— 面板打开时用，延后 + 命中缓存即渲染；
 * - `loadBindings()` —— 数据变更后（解绑）用，`force` 绕缓存重取。
 */
export function useSettingsBindings() {
  const { t } = useI18n()

  const identities = ref<UserIdentity[]>([])
  const bindable = ref<SsoPublicProvider[]>([])
  const bindingsLoading = ref(false)
  const unbinding = ref('')

  /**
   * 三方绑定的两个请求合成一份缓存值（它们只一起用于渲染同一个区块）。
   *
   * **不缓存**：本函数被解绑（`handleUnbind`）在数据变更后直接调用 —— 那条路径要的是
   * "解绑完立刻看到新列表"，走缓存会显示刚解绑的那一行还在。缓存只接在 `onMounted` 上
   * （见 `openBindings`），两者共用同一个 fetcher。
   */
  async function fetchBindings(): Promise<{ ids: UserIdentity[]; providers: SsoPublicProvider[] }> {
    const [ids, providers] = await Promise.all([
      listIdentities(),
      // 可绑定列表取不到不算失败：绑不绑第三方不该被"租户没配 SSO"挡住
      listPublicSsoProviders().catch(() => [] as SsoPublicProvider[]),
    ])
    return { ids, providers }
  }

  /** 把一份绑定结果落到视图上（已绑定的 provider 不再出现在可绑定列表） */
  function applyBindings(ids: UserIdentity[], providers: SsoPublicProvider[]) {
    identities.value = ids
    const boundNames = new Set(ids.map(i => i.provider_name))
    bindable.value = providers.filter(p => !boundNames.has(p.display_name) && !boundNames.has(p.name))
  }

  /** 数据变更后 / 失败重试用：**绕过缓存**重新取，并把新值写回缓存 */
  async function loadBindings() {
    bindingsLoading.value = true
    try {
      const { ids, providers } = await tallyCache.load('identities', fetchBindings, { force: true })
      applyBindings(ids, providers)
    } catch (e) {
      message.error(serverErrorMessage(e, t('errors.failed_to_load_binding_info')))
    } finally {
      bindingsLoading.value = false
    }
  }

  /** 面板打开时用：延后 + 命中缓存即渲染（成本来源见 `onMounted` 处说明） */
  function openBindings() {
    return scheduleTally(
      tallyCache,
      'identities',
      fetchBindings,
      ({ ids, providers }) => {
        applyBindings(ids, providers)
        bindingsLoading.value = false
      },
      {
        onStart: () => { bindingsLoading.value = true },
        onError: (e) => {
          bindingsLoading.value = false
          message.error(serverErrorMessage(e, t('errors.failed_to_load_binding_info')))
        },
      },
    )
  }

  function startBind(providerId: string) {
    // 整页跳转：网关域 cookie 随导航携带，bind state 由后端签发
    window.location.href = ssoLoginURL(providerId, 'bind')
  }

  async function handleUnbind(id: string) {
    unbinding.value = id
    try {
      await deleteIdentity(id)
      message.success(t('common.unbound'))
      await loadBindings()
    } catch (e) {
      message.error(serverErrorMessage(e, t('errors.failed_to_unbind')))
    } finally {
      unbinding.value = ''
    }
  }

  return {
    identities,
    bindable,
    bindingsLoading,
    unbinding,
    loadBindings,
    openBindings,
    startBind,
    handleUnbind,
  }
}
