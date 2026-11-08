<script setup lang="ts">
import AdminTabbedView from '@/components/admin/AdminTabbedView.vue'
import OAuthProvidersView from './OAuthProvidersView.vue'
import PrivacyView from './PrivacyView.vue'

import { useRoute } from 'vue-router'
import { useI18n } from 'vue-i18n'
const { t } = useI18n()
const route = useRoute()
// 认证与防护：三方登录与人机验证 + 隐私模式管控（同属"准入与合规"配置域）
const tabs = [
  { key: 'oauth', label: t('auth.third_party_login_and_human_verification'), comp: OAuthProvidersView },
  { key: 'privacy', label: t('admin.privacy_mode_control'), comp: PrivacyView },
]
</script>

<template>
  <AdminTabbedView
    :route-path="route.path"
    default-tab="oauth"
    :tabs="tabs"
  />
</template>
