<script setup lang="ts">
import { ref, watch, type Component } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { Tabs, TabPane } from 'ant-design-vue'

import { useI18n } from 'vue-i18n'
const { t: tr } = useI18n()
/**
 * 后台「多面板合并页」通用容器。
 *
 * 用途：把若干同类后台页面合并为一个菜单项，用 Tabs 承载（原页面组件原样复用为面板，
 * 无需改动其实现与样式）。
 *
 * 关键行为：
 * 1. 懒加载 —— 只有被访问过的 Tab 才挂载对应面板组件（Tabs 默认会保留已挂载内容），
 *    避免进入页面就并发请求所有面板的端点。这也是合并页相对「每页独立」的主要风险点，
 *    在这里集中处理。
 * 2. URL 同步 —— ?tab= 与激活 Tab 双向绑定：刷新、分享、外部链接（含旧路由重定向）
 *    都能定位到同一个面板；切换 Tab 时用 replace 写回，不污染浏览器历史。
 *
 * 用法：
 *   <AdminTabbedView
 *     route-path="/admin/access"
 *     default-tab="roles"
 *     :tabs="[{ key: 'roles', label: tr('admin.role'), comp: RolesView }, ...]"
 *   />
 */
export interface AdminTabDef {
  key: string
  label: string
  comp: Component
}

const props = defineProps<{
  /** 本页路由路径，用于切换 Tab 时写回 URL */
  routePath: string
  /** 未指定 ?tab= 时使用的面板 */
  defaultTab: string
  tabs: AdminTabDef[]
}>()

const route = useRoute()
const router = useRouter()

function normalizeTab(v: unknown): string {
  const keys = props.tabs.map(t => t.key)
  if (typeof v === 'string' && keys.includes(v)) return v
  return props.defaultTab
}

const activeTab = ref<string>(normalizeTab(route.query.tab))
// 已挂载过的 Tab（懒加载 + 切回不重复请求）
const mountedTabs = ref<Set<string>>(new Set([activeTab.value]))

// 旧路由重定向过来会带 ?tab=，此处跟随
watch(() => route.query.tab, (v) => {
  const t = normalizeTab(v)
  activeTab.value = t
  mountedTabs.value = new Set([...mountedTabs.value, t])
})

watch(activeTab, (t) => {
  mountedTabs.value = new Set([...mountedTabs.value, t])
})

function onTabChange(key: string | number) {
  const t = normalizeTab(key)
  if (t === activeTab.value) return
  activeTab.value = t
  void router.replace({ path: props.routePath, query: { tab: t } })
}
</script>

<template>
  <div class="tabbed-admin-page">
    <Tabs
      :active-key="activeTab"
      @change="onTabChange"
    >
      <TabPane
        v-for="t in tabs"
        :key="t.key"
        :tab="t.label"
      >
        <component
          :is="t.comp"
          v-if="mountedTabs.has(t.key)"
        />
      </TabPane>
    </Tabs>
  </div>
</template>

<style scoped>
.tabbed-admin-page { padding: 0 4px; }
</style>
