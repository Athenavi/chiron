<script setup lang="ts">
import { type Component } from 'vue'

import { Tabs, TabPane } from 'ant-design-vue'
import { useTabbedQuery } from '@/composables/useTabbedQuery'
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

// `?tab=` 同步与"已访问"集合抽到 composable：`/admin/settings` 也要用同一套
// （那边是单文件内的 Tabs，不适合挂这个容器组件）。逻辑只有一份。
const {
  activeTab,
  visited: mountedTabs,
  select: onTabChange,
} = useTabbedQuery({
  routePath: props.routePath,
  defaultTab: props.defaultTab,
  tabKeys: props.tabs.map(t => t.key),
})</script>

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
