<script setup lang="ts">
import { ref, onMounted, computed } from 'vue'
import { useRouter, useRoute } from 'vue-router'
import { Card, Form, FormItem, Input, Button, Alert, Space } from 'ant-design-vue'
import { LockOutlined } from '@ant-design/icons-vue'
import { getCaptchaPublicConfig, confirmPasswordReset } from '../api/auth'
import CaptchaWidget from '../components/CaptchaWidget.vue'
import LanguageSwitcher from '../components/common/LanguageSwitcher.vue'
import type { Rule } from 'ant-design-vue/es/form'

import { useI18n } from 'vue-i18n'
const { t } = useI18n()

// 重置密码：令牌来自邮件里的链接（/reset-password?token=...）。
// 校验规则与后端 ValidatePasswordComplexity 保持一致，避免"前端过了后端拒绝"。

const router = useRouter()
const route = useRoute()

const token = computed(() => String(route.query.token || ''))

const formRef = ref()
const form = ref({ password: '', confirmPassword: '' })

/** 与后端 auth.ValidatePasswordComplexity 对齐的复杂度校验 */
function passwordComplexityError(pw: string): string {
  if (pw.length < 8) return t('auth.password_must_be_at_least_8_characters_2')
  if (pw.length > 128) return t('auth.password_cannot_exceed_128_characters')
  if (!/[A-Z]/.test(pw)) return t('auth.password_must_contain_an_uppercase_letter')
  if (!/[a-z]/.test(pw)) return t('auth.password_must_contain_a_lowercase_letter')
  if (!/\d/.test(pw)) return t('auth.password_must_contain_a_digit')
  if (!/[^A-Za-z0-9]/.test(pw)) return t('密码必须包含特殊字符（如 {chars}）', { chars: '!@#$%^&*' })
  return ''
}

const rules: Record<string, Rule[]> = {
  password: [
    { required: true, message: t('auth.please_enter_a_new_password'), trigger: 'blur' },
    {
      validator: (_rule: Rule, value: string) => {
        const msg = passwordComplexityError(value || '')
        return msg ? Promise.reject(msg) : Promise.resolve()
      },
      trigger: 'blur',
    },
  ],
  confirmPassword: [
    { required: true, message: t('auth.please_enter_the_new_password_again'), trigger: 'blur' },
    {
      validator: (_rule: Rule, value: string) => {
        if (value !== form.value.password) {
          return Promise.reject(t('auth.the_two_password_entries_do_not_match'))
        }
        return Promise.resolve()
      },
      trigger: 'blur',
    },
  ],
}

const error = ref('')
const done = ref(false)
const loading = ref(false)

const captchaConfig = ref({ enabled: false, provider: '', site_key: '', verify_url: '' })
const captchaRequired = ref(false)
const captchaToken = ref('')
const captchaRandstr = ref('')
const captchaRef = ref<InstanceType<typeof CaptchaWidget>>()

function markCaptchaDirty() {
  captchaToken.value = ''
  captchaRandstr.value = ''
}

onMounted(async () => {
  if (!token.value) {
    error.value = t('auth.reset_link_invalid_please_reapply')
  }
  try {
    const cfg = await getCaptchaPublicConfig()
    captchaConfig.value = {
      enabled: !!cfg.enabled,
      provider: cfg.provider || '',
      site_key: cfg.site_key || '',
      verify_url: cfg.verify_url || '',
    }
  } catch {
    // 配置接口不可达时按无验证码处理（后端仍会兜底校验）
  }
  captchaRequired.value = captchaConfig.value.enabled
})

async function handleSubmit() {
  error.value = ''
  if (!token.value) {
    error.value = t('auth.reset_link_invalid_please_reapply')
    return
  }
  try {
    await formRef.value?.validate()
  } catch {
    return
  }
  if (captchaRequired.value && captchaConfig.value.provider !== 'custom' && !captchaToken.value) {
    error.value = t('auth.please_complete_the_human_verification_first')
    return
  }
  loading.value = true
  try {
    await confirmPasswordReset({
      token: token.value,
      password: form.value.password,
      captcha_token: captchaToken.value,
      captcha_randstr: captchaRandstr.value,
    })
    done.value = true
    setTimeout(() => router.push('/login'), 1500)
  } catch (e: any) {
    const status = e.response?.status
    const apiErr = e.response?.data?.error
    if (status === 428 || apiErr === 'captcha_required') {
      captchaRequired.value = true
      error.value = t('auth.too_many_requests_please_complete_human_verification_and_retry')
      captchaRef.value?.reset()
      markCaptchaDirty()
      return
    }
    if (status === 400) {
      // 令牌失效（过期 / 已用过）与密码不合规都走这里
      error.value = apiErr || t('auth.reset_link_expired_please_reapply')
      return
    }
    error.value = apiErr || t('auth.reset_failed_please_try_again_later')
  } finally {
    loading.value = false
  }
}
</script>

<template>
  <div class="auth-container">
  <div class="auth-card">
    <!-- 语言切换：重置密码前即可选择界面语言 -->
    <div style="display: flex; justify-content: flex-end; margin-block-end: 8px">
      <LanguageSwitcher />
    </div>
    <div class="auth-header">
        <div class="auth-logo">
          MC
        </div>
        <div class="auth-title">
          {{ $t('auth.set_new_password') }}
        </div>
        <div class="auth-subtitle">
          {{ $t('auth.please_set_a_new_login_password') }}
        </div>
      </div>
      <Card
        :bordered="false"
        class="auth-form-card"
      >
        <Alert
          v-if="done"
          type="success"
          :message="$t('auth.password_reset_redirecting_to_login')"
          show-icon
          style="margin-bottom: 16px"
        />
        <Alert
          v-if="error"
          type="error"
          :message="error"
          show-icon
          style="margin-bottom: 16px"
        />

        <Form
          v-if="!done"
          ref="formRef"
          :model="form"
          :rules="rules"
          layout="vertical"
          @finish="handleSubmit"
        >
          <FormItem
            :label="$t('auth.new_password')"
            name="password"
          >
            <Input
              v-model:value="form.password"
              :placeholder="$t('common.at_least_8_characters_with_upper_lowercase_letters_digits_and_special_characters')"
              type="password"
              size="large"
              :aria-label="$t('auth.newPassword')"
              autocomplete="new-password"
            >
              <template #prefix>
                <LockOutlined />
              </template>
            </Input>
          </FormItem>

          <FormItem
            :label="$t('auth.confirm_new_password')"
            name="confirmPassword"
          >
            <Input
              v-model:value="form.confirmPassword"
              :placeholder="$t('auth.please_enter_the_new_password_again')"
              type="password"
              size="large"
              :aria-label="$t('auth.confirmNewPassword')"
              autocomplete="new-password"
            >
              <template #prefix>
                <LockOutlined />
              </template>
            </Input>
          </FormItem>

          <FormItem
            v-if="captchaRequired"
            :label="$t('auth.human_verification')"
          >
            <CaptchaWidget
              ref="captchaRef"
              :provider="captchaConfig.provider"
              :site-key="captchaConfig.site_key"
              :verify-url="captchaConfig.verify_url"
              @verified="(p: any) => { captchaToken = p.token; captchaRandstr = p.randstr || '' }"
              @expired="markCaptchaDirty"
            />
          </FormItem>

          <FormItem>
            <Space
              direction="vertical"
              style="width: 100%"
            >
              <Button
                type="primary"
                html-type="submit"
                block
                size="large"
                :loading="loading"
              >
                {{ $t('auth.confirm_reset') }}
              </Button>
              <Button
                type="link"
                block
                @click="router.push('/login')"
              >
                {{ $t('auth.back_to_sign_in') }}
              </Button>
            </Space>
          </FormItem>
        </Form>
      </Card>
    </div>
  </div>
</template>

<style scoped>
.auth-container {
  display: flex;
  justify-content: center;
  align-items: center;
  min-height: 100dvh;
  padding: 24px 16px;
  background: var(--bg-page);
  position: relative;
  overflow: hidden;
}

.auth-container::before {
  content: '';
  position: absolute;
  inset: -45% -20% auto -20%;
  height: 60%;
  background: radial-gradient(ellipse 55% 55% at 50% 0%, var(--primary-bg), transparent 72%);
  pointer-events: none;
}

.auth-card {
  width: 420px;
  max-width: calc(100vw - 32px);
  position: relative;
  z-index: var(--z-content);
  animation: authFadeIn var(--dur-slow) ease;
}

.auth-form-card {
  border-radius: var(--radius-lg) !important;
  border: 1px solid var(--border-card);
  box-shadow: var(--shadow-lg);
  background: var(--bg-card);
}

.auth-header { text-align: center; margin-bottom: 24px; }

.auth-logo {
  width: 44px;
  height: 44px;
  margin: 0 auto 14px;
  border-radius: 12px;
  background: linear-gradient(135deg, var(--primary), var(--primary-dark));
  color: var(--on-solid);
  font-weight: 700;
  font-size: 16px;
  display: flex;
  align-items: center;
  justify-content: center;
  box-shadow: var(--shadow-md);
}

.auth-title {
  font-size: 22px;
  font-weight: 650;
  color: var(--text-primary);
}

.auth-subtitle {
  font-size: 14px;
  color: var(--text-tertiary);
  margin-top: 4px;
}

@keyframes authFadeIn {
  from { opacity: 0; transform: translateY(20px); }
  to { opacity: 1; transform: translateY(0); }
}

@media (max-width: 576px) {
  .auth-container { align-items: flex-start; padding: 16px 12px; }
  .auth-card { width: 100%; max-width: 100%; }
  .auth-form-card :deep(.ant-card-body) { padding: 20px 16px; }
  .auth-form-card :deep(.ant-input) { font-size: 16px; }
  .auth-form-card :deep(.ant-btn:not(.ant-btn-sm)) { min-height: 40px; }
}

@media (prefers-reduced-motion: reduce) {
  .auth-card { animation: none; }
}
</style>
