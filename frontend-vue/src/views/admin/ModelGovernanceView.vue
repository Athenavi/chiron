<script setup lang="ts">
import AdminTabbedView from '@/components/admin/AdminTabbedView.vue'
import ModelPolicyView from './ModelPolicyView.vue'
import ModelRouterView from './ModelRouterView.vue'

import { useI18n } from 'vue-i18n'
const { t } = useI18n()
// 模型管控：策略（可用模型/额度约束）+ 路由（provider 路由规则）
const tabs = [
  { key: 'policy', label: t('agent.model_policy'), comp: ModelPolicyView },
  { key: 'routing', label: t('agent.model_routing'), comp: ModelRouterView },
]
</script>

<template>
  <AdminTabbedView
    route-path="/admin/models"
    default-tab="policy"
    :tabs="tabs"
  />
</template>
