<script setup lang="ts">
import AdminTabbedView from '@/components/admin/AdminTabbedView.vue'
import TenantManagementView from './TenantManagementView.vue'
import DomainManagementView from './DomainManagementView.vue'

import { useI18n } from 'vue-i18n'
const { t } = useI18n()
// 租户与域名：租户（配额/暂停/用量）+ 域名（SSL/校验）—— 同为 admin 权限域的资源管理
const tabs = [
  { key: 'tenants', label: t('admin.tenant'), comp: TenantManagementView },
  { key: 'domains', label: t('admin.domain'), comp: DomainManagementView },
]
</script>

<template>
  <AdminTabbedView
    route-path="/admin/tenancy"
    default-tab="tenants"
    :tabs="tabs"
  />
</template>
