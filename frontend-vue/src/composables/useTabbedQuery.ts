/**
 * 后台多面板页的 **`?tab=` 同步**（从 `components/admin/AdminTabbedView.vue` 抽出）。
 *
 * 为什么抽出来：本轮要让 `/admin/settings` 也用 Tab 承载（它此前是一张 11 卡片的长页），
 * 而"URL 与激活 Tab 双向绑定 + 只挂载访问过的面板"这套逻辑**只能有一份** ——
 * 复制一遍就等于以后修一处漏一处（`AdminTabbedView` 的文档注释里也是把它当作
 * 「合并页相对每页独立的主要风险点，集中处理」）。
 *
 * 行为（与抽取前逐条一致）：
 *   1. 初始值取 `?tab=`，非法值或缺失时回落 `defaultTab`；
 *   2. `?tab=` 变化时跟随（旧路由重定向过来会带 query）；
 *   3. 切换时用 `router.replace` 写回，**不污染浏览器历史**；
 *   4. `visited` 只增不减 —— 由调用方据此决定"面板是否已挂载"（懒加载 + 切回不重复请求）。
 */
import { ref, watch, type Ref } from 'vue'
import { useRoute, useRouter } from 'vue-router'

export interface TabbedQueryOptions {
  /** 本页路由路径，用于切换 Tab 时写回 URL */
  routePath: string
  /** 未指定 ?tab=（或值非法）时使用的面板 */
  defaultTab: string
  /** 合法 Tab key 列表（顺序无关，只用于校验） */
  tabKeys: readonly string[]
}

export interface TabbedQueryState {
  /** 当前激活的 Tab（可写：`@change` 时交给 `select` 更稳妥，直接赋值也不会写 URL） */
  activeTab: Ref<string>
  /** 已访问过的 Tab 集合（懒加载依据） */
  visited: Ref<Set<string>>
  /** 切换 Tab：更新状态并 `replace` 写回 `?tab=` */
  select: (key: string | number) => void
}

export function useTabbedQuery(opts: TabbedQueryOptions): TabbedQueryState {
  const route = useRoute()
  const router = useRouter()

  function normalizeTab(v: unknown): string {
    if (typeof v === 'string' && opts.tabKeys.includes(v)) return v
    return opts.defaultTab
  }

  const activeTab = ref<string>(normalizeTab(route.query.tab))
  // 已访问过的 Tab（切换时只增不减）
  const visited = ref<Set<string>>(new Set([activeTab.value]))

  watch(activeTab, (t) => {
    visited.value = new Set([...visited.value, t])
  })

  // 旧路由重定向过来会带 ?tab=，此处跟随
  watch(() => route.query.tab, (v) => {
    activeTab.value = normalizeTab(v)
  })

  function select(key: string | number): void {
    const t = normalizeTab(key)
    if (t === activeTab.value) return
    activeTab.value = t
    void router.replace({ path: opts.routePath, query: { tab: t } })
  }

  return { activeTab, visited, select }
}
