<script setup lang="ts">
import { ref, onMounted } from 'vue'
import { useRouter } from 'vue-router'
import { Card, Form, FormItem, Input, Button, Alert, Space } from 'ant-design-vue'
import { MailOutlined, LockOutlined, UserOutlined, SafetyOutlined } from '@ant-design/icons-vue'
import { useAuthStore } from '../stores/auth'
import { getCaptchaPublicConfig, getEmailStatus, sendEmailCode, isValidEmail } from '../api/auth'
import { useSmsCountdown } from '../composables/useSmsCountdown'
import CaptchaWidget from '../components/CaptchaWidget.vue'
import LanguageSwitcher from '../components/common/LanguageSwitcher.vue'
import type { Rule } from 'ant-design-vue/es/form'

import { useI18n } from 'vue-i18n'
const { t } = useI18n()

const router = useRouter()
const authStore = useAuthStore()

const formRef = ref()
const form = ref({
  name: '',
  email: '',
  password: '',
  confirmPassword: '',
  emailCode: '',
})

const rules: Record<string, Rule[]> = {
  name: [{ required: true, message: t('common.please_enter_your_name'), trigger: 'blur' }],
  email: [
    { required: true, message: t('mail.please_enter_email'), trigger: 'blur' },
    { type: 'email', message: t('errors.invalid_email_format'), trigger: 'blur' },
  ],
  password: [
    { required: true, message: t('auth.please_enter_password'), trigger: 'blur' },
    { min: 8, message: t('auth.password_must_be_at_least_8_characters'), trigger: 'blur' },
  ],
  confirmPassword: [
    { required: true, message: t('auth.please_confirm_the_password'), trigger: 'blur' },
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

// ── 人机验证（注册接口防刷） ──
const captchaConfig = ref({ enabled: false, provider: '', site_key: '', verify_url: '' })
const captchaRequired = ref(false)
const captchaToken = ref('')
const captchaRandstr = ref('')
const captchaRef = ref<InstanceType<typeof CaptchaWidget>>()

function markCaptchaDirty() {
  captchaToken.value = ''
  captchaRandstr.value = ''
}

// ── 邮箱验证码（后台开启「注册邮箱验证」时必填） ──
const emailVerifyRequired = ref(false)
const emailSending = ref(false)
const emailCodeError = ref('')
const { remaining: emailRemaining, start: startEmailCountdown } = useSmsCountdown(60)

const emailCodeRules: Rule[] = [
  { required: true, message: t('auth.please_enter_email_verification_code'), trigger: 'blur' },
  { len: 6, message: t('auth.verification_code_is_a_6_digit_number'), trigger: 'blur' },
]

async function handleSendEmailCode() {
  emailCodeError.value = ''
  const email = form.value.email.trim()
  if (!isValidEmail(email)) {
    emailCodeError.value = t('mail.please_enter_a_valid_email_first')
    return
  }
  if (captchaRequired.value && captchaConfig.value.provider !== 'custom' && !captchaToken.value) {
    emailCodeError.value = t('auth.please_complete_the_human_verification_first')
    return
  }
  emailSending.value = true
  try {
    const res = await sendEmailCode({
      email,
      purpose: 'register',
      captcha_token: captchaToken.value,
      captcha_randstr: captchaRandstr.value,
    })
    // 以服务端冷却为准（0 表示不冷却，不倒计时）
    startEmailCountdown(res.interval ?? 0)
    emailCodeError.value = ''
    markCaptchaDirty()
    captchaRef.value?.reset()
  } catch (e: any) {
    const status = e.response?.status
    const apiErr = e.response?.data?.error
    if (status === 428 || apiErr === 'captcha_required') {
      captchaRequired.value = true
      emailCodeError.value = t('auth.too_many_requests_please_complete_human_verification_and_retry')
      captchaRef.value?.reset()
      markCaptchaDirty()
      return
    }
    if (status === 403 && String(apiErr).includes('captcha')) {
      emailCodeError.value = t('auth.human_verification_failed_please_verify_again')
      captchaRef.value?.reset()
      markCaptchaDirty()
      return
    }
    if (status === 429) {
      emailCodeError.value = t('common.sending_too_frequently_please_try_again_later')
      return
    }
    emailCodeError.value = apiErr || t('auth.verificationCodeSendFailed')
  } finally {
    emailSending.value = false
  }
}

onMounted(async () => {
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
  try {
    const es = await getEmailStatus()
    emailVerifyRequired.value = !!(es.enabled && es.register_verify)
  } catch {
    // 邮件服务状态不可达时隐藏验证码输入（后端仍会兜底要求）
  }
})

async function handleRegister() {
  error.value = ''
  try {
    await formRef.value?.validate()
  } catch {
    return
  }
  if (captchaRequired.value && captchaConfig.value.provider !== 'custom' && !captchaToken.value) {
    error.value = t('auth.please_complete_the_human_verification_first')
    return
  }
  if (emailVerifyRequired.value && !form.value.emailCode.trim()) {
    error.value = t('auth.please_get_and_fill_in_the_email_verification_code_first')
    return
  }
  try {
    await authStore.register(
      form.value.email,
      form.value.password,
      form.value.name,
      { token: captchaToken.value, randstr: captchaRandstr.value },
      form.value.emailCode.trim(),
    )
    router.push('/chat')
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
    if (status === 403 && String(apiErr).includes('captcha')) {
      error.value = t('auth.human_verification_failed_please_verify_again')
      captchaRef.value?.reset()
      markCaptchaDirty()
      return
    }
    error.value = apiErr || t('auth.registerFailed')
  }
}
</script>

<template>
  <div class="register-container">
  <div class="register-card">
    <!-- 语言切换：注册前即可选择界面语言（RTL 语言由 <html dir> 自动生效） -->
    <div style="display: flex; justify-content: flex-end; margin-block-end: 8px">
      <LanguageSwitcher />
    </div>
    <div class="register-header">
        <div class="register-logo">
          MC
        </div>
        <div class="register-title">
          {{ $t('auth.create_account') }}
        </div>
        <div class="register-subtitle">
          {{ $t('agent.join_chiron_ai_agent_platform') }}
        </div>
      </div>
      <Card
        :bordered="false"
        class="register-form-card"
      >
        <Alert
          v-if="error"
          type="error"
          :message="error"
          show-icon
          style="margin-bottom: 16px"
        />

        <Alert
          type="info"
          :message="$t('auth.if_the_system_has_no_accounts_yet_this_registration_will_become_the_system_admin_owner')"
          show-icon
          style="margin-bottom: 16px"
        />

        <Form
          ref="formRef"
          :model="form"
          :rules="rules"
          layout="vertical"
          @finish="handleRegister"
        >
          <FormItem
            :label="$t('common.name_3')"
            name="name"
          >
            <Input
              v-model:value="form.name"
              :placeholder="$t('common.please_enter_your_name')"
              size="large"
              :aria-label="$t('common.name_3')"
              autocomplete="name"
            >
              <template #prefix>
                <UserOutlined />
              </template>
            </Input>
          </FormItem>

          <FormItem
            :label="$t('auth.email')"
            name="email"
          >
            <Input
              v-model:value="form.email"
              :placeholder="$t('mail.please_enter_email')"
              size="large"
              :aria-label="$t('auth.email')"
              autocomplete="email"
            >
              <template #prefix>
                <MailOutlined />
              </template>
            </Input>
          </FormItem>

          <FormItem
            v-if="emailVerifyRequired"
            :label="$t('auth.email_verification_code')"
            name="emailCode"
            :rules="emailCodeRules"
            :help="emailCodeError"
            :validate-status="emailCodeError ? 'error' : undefined"
          >
            <Input
              v-model:value="form.emailCode"
              :placeholder="$t('auth.please_enter_the_verification_code_received_by_email')"
              size="large"
              :maxlength="6"
              :aria-label="$t('auth.email_verification_code')"
              autocomplete="one-time-code"
            >
              <template #prefix>
                <SafetyOutlined />
              </template>
              <template #suffix>
                <Button
                  size="small"
                  type="link"
                  :disabled="emailRemaining > 0 || emailSending"
                  :loading="emailSending"
                  @click="handleSendEmailCode"
                >
                  {{ emailRemaining > 0 ? $t('auth.resendCountdown', { s: emailRemaining }) : $t('auth.get_verification_code') }}
                </Button>
              </template>
            </Input>
          </FormItem>

          <FormItem
            :label="$t('auth.password')"
            name="password"
          >
            <Input
              v-model:value="form.password"
              :placeholder="$t('auth.please_enter_password_at_least_8_characters')"
              type="password"
              size="large"
              :aria-label="$t('auth.password')"
              autocomplete="new-password"
            >
              <template #prefix>
                <LockOutlined />
              </template>
            </Input>
          </FormItem>

          <FormItem
            :label="$t('auth.confirmPassword')"
            name="confirmPassword"
          >
            <Input
              v-model:value="form.confirmPassword"
              :placeholder="$t('auth.please_enter_password_again')"
              type="password"
              size="large"
              :aria-label="$t('auth.confirmPassword')"
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
                :loading="authStore.loading"
                size="large"
              >
                {{ $t('auth.register') }}
              </Button>
              <Button
                type="link"
                block
                @click="router.push('/login')"
              >
                {{ $t('auth.have_an_account_log_in') }}
              </Button>
            </Space>
          </FormItem>
        </Form>
      </Card>
    </div>
  </div>
</template>

<style scoped>
/* 中性底色 + 顶部微弱 accent 光晕（与登录页一致，带 AI 紫渐变） */
.register-container {
  display: flex;
  justify-content: center;
  align-items: center;
  min-height: 100dvh;
  padding: 24px 16px;
  background: var(--bg-page);
  position: relative;
  overflow: hidden;
}

.register-container::before {
  content: '';
  position: absolute;
  inset: -45% -20% auto -20%;
  height: 60%;
  background: radial-gradient(ellipse 55% 55% at 50% 0%, var(--primary-bg), transparent 72%);
  pointer-events: none;
}

.register-card {
  width: 420px;
  max-width: calc(100vw - 32px);
  position: relative;
  z-index: var(--z-content);
  animation: registerFadeIn var(--dur-slow) ease;
}

.register-form-card {
  border-radius: var(--radius-lg) !important;
  border: 1px solid var(--border-card);
  box-shadow: var(--shadow-lg);
  background: var(--bg-card);
}

.register-header {
  text-align: center;
  margin-bottom: 24px;
}

.register-logo {
  width: 44px;
  height: 44px;
  margin: 0 auto 14px;
  border-radius: 12px;
  background: linear-gradient(135deg, var(--primary), var(--primary-dark));
  color: var(--on-solid);
  font-weight: 700;
  font-size: 16px;
  letter-spacing: 0.02em;
  display: flex;
  align-items: center;
  justify-content: center;
  box-shadow: var(--shadow-md);
}

.register-title {
  font-size: 22px;
  font-weight: 650;
  color: var(--text-primary);
  letter-spacing: -0.01em;
}

.register-subtitle {
  font-size: 14px;
  color: var(--text-tertiary);
  margin-top: 4px;
}

@keyframes registerFadeIn {
  from { opacity: 0; transform: translateY(20px); }
  to { opacity: 1; transform: translateY(0); }
}

/* 移动端（≤576px）：小屏顶部对齐，便于长表单滚动 */
@media (max-width: 576px) {
  .register-container { align-items: flex-start; padding: 16px 12px; }
  .register-card { width: 100%; max-width: 100%; }
  .register-logo { width: 40px; height: 40px; margin-bottom: 10px; }
  .register-title { font-size: 20px; }
  .register-subtitle { font-size: 13px; }
  .register-header { margin-bottom: 18px; }
  /* 表单贴边：卡片内边距收窄，让输入框更接近屏幕边缘 */
  .register-form-card :deep(.ant-card-body) { padding: 20px 16px; }
  /* iOS 聚焦防缩放：输入字号 ≥16px */
  .register-form-card :deep(.ant-input) { font-size: 16px; }
  /* 触控目标 ≥40px */
  .register-form-card :deep(.ant-btn:not(.ant-btn-sm)) { min-height: 40px; }
}

/* 焦点可见性增强（键盘导航） */
.register-form-card :deep(.ant-input:focus),
.register-form-card :deep(.ant-input:focus-within),
.register-form-card :deep(.ant-btn:focus-visible) {
  outline: 2px solid var(--primary);
  outline-offset: 2px;
}

@media (prefers-reduced-motion: reduce) {
  .register-card { animation: none; }
}
</style>