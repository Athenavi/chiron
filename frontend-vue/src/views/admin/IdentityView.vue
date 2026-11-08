<script setup lang="ts">
import AdminTabbedView from '@/components/admin/AdminTabbedView.vue'
import SsoProvidersSection from '../tenant/identity/SsoProvidersSection.vue'
import CaptchaSection from '../tenant/identity/CaptchaSection.vue'
import SmsSection from '../tenant/identity/SmsSection.vue'
import PrivacyView from './PrivacyView.vue'

import { useRoute } from 'vue-router'
import { useI18n } from 'vue-i18n'
const { t } = useI18n()
const route = useRoute()
// 认证与防护拆成四个独立面板：三方登录、人机验证、短信、隐私策略。
const tabs = [
  { key: 'sso', label: t('auth.third_party_login'), comp: SsoProvidersSection },
  { key: 'captcha', label: t('auth.human_verification'), comp: CaptchaSection },
  { key: 'sms', label: t('auth.sms_service_captcha_login'), comp: SmsSection },
  { key: 'privacy', label: t('admin.privacy_mode_control'), comp: PrivacyView },
]
</script>

<template>
  <AdminTabbedView
    :route-path="route.path"
    default-tab="sso"
    :tabs="tabs"
  />
</template>
