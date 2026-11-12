<script setup lang="ts">
import { computed } from 'vue'
import AdminTabbedView from '@/components/admin/AdminTabbedView.vue'
import TenantManagementView from './TenantManagementView.vue'
import DomainManagementView from './DomainManagementView.vue'
import { useAuthStore } from '@/stores/auth'

import { useI18n } from 'vue-i18n'
const { t } = useI18n()
const authStore = useAuthStore()

/**
 * 租户与域名：租户（配额/暂停/用量）+ 域名（SSL/校验）。
 *
 * 两个面板的权限**不同级**：`tenants` 走 `platformReadMW`（仅平台 owner），
 * `domains` 走 `adminReadMW`（租户 admin 也有）。所以不能整页按角色藏掉，
 * 否则会连带剥夺 admin 的域名管理能力 —— 这里按角色裁面板。
 *
 * `defaultTab` 必须跟着裁：admin 若落在 `tenants` 面板，除了 403 什么也看不到。
 * 角色在进页面前已由路由守卫拉过权威 profile，setup 时求值一次是安全的。
 */
const tabs = computed(() => {
  const all = [
    { key: 'tenants', label: t('admin.tenant'), comp: TenantManagementView, ownerOnly: true },
    { key: 'domains', label: t('admin.domain'), comp: DomainManagementView, ownerOnly: false },
  ]
  return all
    .filter(tab => !tab.ownerOnly || authStore.isOwner)
    // 显式挑字段而不是解构丢弃：解构出的 `ownerOnly` 会触发 no-unused-vars
    .map(tab => ({ key: tab.key, label: tab.label, comp: tab.comp }))
})

const defaultTab = computed(() => (authStore.isOwner ? 'tenants' : 'domains'))
</script>

<template>
  <AdminTabbedView
    route-path="/admin/tenancy"
    :default-tab="defaultTab"
    :tabs="tabs"
  />
</template>
