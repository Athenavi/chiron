import { t } from '../i18n'

import { ref, type Ref } from 'vue'

/**
 * 通用 CRUD 资源加载 composable。
 *
 * 封装 Admin 视图中重复出现的加载样板：
 *   loading ref + try/catch + finally 清除 loading + console.error
 *
 * 用法：
 *   const { data: domains, loading, load: loadDomains } = useCrudResource(
 *     [],
 *     async () => (await api.get('/admin/domains')).data?.data || []
 *   )
 *   onMounted(loadDomains)
 *
 * 行为约定（与重构前各视图保持一致）：
 * - loading 初始为 true，load 结束时置 false
 * - 加载失败时保留上一次的 data，仅记录 error 并 console.error
 * - loader 为闭包，可读取视图中的筛选/分页状态，reload 即再次调用 load
 */
export function useCrudResource<T>(initialData: T, loader: () => Promise<T>) {
  const data = ref(initialData) as Ref<T>
  const loading = ref(true)
  const error = ref<string | null>(null)

  async function load(): Promise<void> {
    loading.value = true
    error.value = null
    try {
      data.value = await loader()
    } catch (e) {
      error.value = apiErrorMessage(e, t('加载失败'))
    } finally {
      loading.value = false
    }
  }

  return { data, loading, error, load, reload: load }
}

/**
 * 从 axios 错误中提取后端错误信息（error.response.data.error），
 * 无法提取时返回兜底文案。统一各视图 alert 的取值逻辑。
 */
export function apiErrorMessage(error: unknown, fallback: string): string {
  const message = (error as any)?.response?.data?.error
  return typeof message === 'string' && message ? message : fallback
}
