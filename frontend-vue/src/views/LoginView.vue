<script setup lang="ts">
import { ref, onMounted } from 'vue'
import { useRouter, useRoute } from 'vue-router'
import { Card, Form, FormItem, Input, Button, Alert, Space, Tabs, TabPane, message } from 'ant-design-vue'
import { MailOutlined, LockOutlined, MobileOutlined, SafetyOutlined } from '@ant-design/icons-vue'
import { useAuthStore } from '../stores/auth'
import { getCaptchaPublicConfig, getSmsStatus, sendSmsCode, smsLogin, isValidPhone, getEmailStatus, sendEmailCode, emailLogin, isValidEmail } from '../api/auth'
import { useSmsCountdown } from '../composables/useSmsCountdown'
import CaptchaWidget from '../components/CaptchaWidget.vue'
import SsoLoginButtons from '../components/SsoLoginButtons.vue'
import LanguageSwitcher from '../components/common/LanguageSwitcher.vue'
import type { Rule } from 'ant-design-vue/es/form'

import { useI18n } from 'vue-i18n'
const { t } = useI18n()
const router = useRouter()
const route = useRoute()
const authStore = useAuthStore()

// ── 密码登录 ──

const formRef = ref()
const form = ref({
  email: '',
  password: '',
})

const rules: Record<string, Rule[]> = {
  email: [
    { required: true, message: t('mail.please_enter_email'), trigger: 'blur' },
    { type: 'email', message: t('errors.invalid_email_format'), trigger: 'blur' },
  ],
  password: [
    { required: true, message: t('auth.please_enter_password'), trigger: 'blur' },
    { min: 6, message: t('auth.password_must_be_at_least_6_characters'), trigger: 'blur' },
  ],
}

const error = ref('')

// ── 人机验证（管理员启用或同 IP 失败升级时要求；两个标签页共用） ──

const captchaConfig = ref({ enabled: false, provider: '', site_key: '', verify_url: '' })
const captchaToken = ref('')
const captchaRandstr = ref('')
const captchaRef = ref<InstanceType<typeof CaptchaWidget>>()

const needCaptcha = ref(false)

function markCaptchaDirty() {
  captchaToken.value = ''
  captchaRandstr.value = ''
}

// ── 短信登录 ──

const smsEnabled = ref(false)
const activeTab = ref('password')

const smsFormRef = ref()
const smsForm = ref({ phone: '', code: '' })

const smsRules: Record<string, Rule[]> = {
  phone: [
    { required: true, message: t('common.please_enter_phone_number'), trigger: 'blur' },
    {
      validator: (_rule: Rule, value: string) =>
        !value || isValidPhone(value) ? Promise.resolve() : Promise.reject(t('errors.invalid_phone_number_format')),
      trigger: 'blur',
    },
  ],
  code: [
    { required: true, message: t('auth.please_enter_the_verification_code'), trigger: 'blur' },
    { len: 6, message: t('auth.verification_code_is_a_6_digit_number'), trigger: 'blur' },
  ],
}

const { remaining, start } = useSmsCountdown(60)
const sending = ref(false)
const smsLoading = ref(false)
const smsError = ref('')

async function handleSendCode() {
  smsError.value = ''
  const phone = smsForm.value.phone.trim()
  if (!isValidPhone(phone)) {
    smsError.value = t('common.please_enter_a_valid_phone_number')
    return
  }
  if (needCaptcha.value && captchaConfig.value.provider !== 'custom' && !captchaToken.value) {
    smsError.value = t('auth.please_complete_the_human_verification_first')
    return
  }
  sending.value = true
  try {
    const res = await sendSmsCode({
      phone,
      purpose: 'login',
      captcha_token: captchaToken.value,
      captcha_randstr: captchaRandstr.value,
    })
    // 倒计时严格对齐后端的冷却秒数：interval=0 表示后端未设冷却（允许连发），
    // 此时不应自作主张锁 60 秒（`|| 60` 会把 0 当成缺省值，造成"多出来"的倒计时）。
    start(res.interval ?? 0)
    message.success(t('auth.verificationCodeSent'))
    // 一次性凭据：发送后重置，登录时按需重新验证
    markCaptchaDirty()
    captchaRef.value?.reset()
  } catch (e: any) {
    const status = e.response?.status
    const apiErr = e.response?.data?.error
    if (status === 428 || apiErr === 'captcha_required') {
      needCaptcha.value = true
      smsError.value = t('auth.too_many_requests_please_complete_human_verification_and_retry')
      captchaRef.value?.reset()
      markCaptchaDirty()
      return
    }
    if (status === 403 && String(apiErr).includes('captcha')) {
      smsError.value = t('auth.human_verification_failed_please_verify_again')
      captchaRef.value?.reset()
      markCaptchaDirty()
      return
    }
    if (status === 429) {
      smsError.value = t('common.sending_too_frequently_please_try_again_later')
      return
    }
    smsError.value = apiErr || t('auth.verificationCodeSendFailed')
  } finally {
    sending.value = false
  }
}

async function handleSmsLogin() {
  smsError.value = ''
  try {
    await smsFormRef.value?.validate()
  } catch {
    return
  }
  if (needCaptcha.value && captchaConfig.value.provider !== 'custom' && !captchaToken.value) {
    smsError.value = t('auth.please_complete_the_human_verification_first')
    return
  }
  smsLoading.value = true
  try {
    const { token, user } = await smsLogin({
      phone: smsForm.value.phone.trim(),
      code: smsForm.value.code.trim(),
      captcha_token: captchaToken.value,
      captcha_randstr: captchaRandstr.value,
    })
    authStore.applySession(token, user)
    router.push('/chat')
  } catch (e: any) {
    const status = e.response?.status
    const apiErr = e.response?.data?.error
    if (status === 428 || apiErr === 'captcha_required') {
      needCaptcha.value = true
      smsError.value = t('auth.too_many_requests_please_complete_human_verification_and_retry')
      captchaRef.value?.reset()
      markCaptchaDirty()
      return
    }
    if (status === 403 && String(apiErr).includes('captcha')) {
      smsError.value = t('auth.human_verification_failed_please_verify_again')
      captchaRef.value?.reset()
      markCaptchaDirty()
      return
    }
    smsError.value = apiErr || t('auth.loginFailed')
  } finally {
    smsLoading.value = false
  }
}

// ── 邮箱验证码登录 ──

const emailEnabled = ref(false)
const resetEnabled = ref(false)

const emailFormRef = ref()
const emailForm = ref({ email: '', code: '' })

const emailRules: Record<string, Rule[]> = {
  email: [
    { required: true, message: t('mail.please_enter_email'), trigger: 'blur' },
    {
      validator: (_rule: Rule, value: string) =>
        !value || isValidEmail(value) ? Promise.resolve() : Promise.reject(t('errors.invalid_email_format')),
      trigger: 'blur',
    },
  ],
  code: [
    { required: true, message: t('auth.please_enter_the_verification_code'), trigger: 'blur' },
    { len: 6, message: t('auth.verification_code_is_a_6_digit_number'), trigger: 'blur' },
  ],
}

// 倒计时逻辑与短信共用（纯计时，不区分通道）
const { remaining: emailRemaining, start: startEmailCountdown } = useSmsCountdown(60)
const emailSending = ref(false)
const emailLoading = ref(false)
const emailError = ref('')

async function handleSendEmailCode() {
  emailError.value = ''
  const email = emailForm.value.email.trim()
  if (!isValidEmail(email)) {
    emailError.value = t('mail.please_enter_a_valid_email')
    return
  }
  if (needCaptcha.value && captchaConfig.value.provider !== 'custom' && !captchaToken.value) {
    emailError.value = t('auth.please_complete_the_human_verification_first')
    return
  }
  emailSending.value = true
  try {
    const res = await sendEmailCode({
      email,
      purpose: 'login',
      captcha_token: captchaToken.value,
      captcha_randstr: captchaRandstr.value,
    })
    // 同上：以服务端冷却为准，0 表示不冷却
    startEmailCountdown(res.interval ?? 0)
    message.success(t('auth.verificationCodeSent'))
    markCaptchaDirty()
    captchaRef.value?.reset()
  } catch (e: any) {
    const status = e.response?.status
    const apiErr = e.response?.data?.error
    if (status === 428 || apiErr === 'captcha_required') {
      needCaptcha.value = true
      emailError.value = t('auth.too_many_requests_please_complete_human_verification_and_retry')
      captchaRef.value?.reset()
      markCaptchaDirty()
      return
    }
    if (status === 403 && String(apiErr).includes('captcha')) {
      emailError.value = t('auth.human_verification_failed_please_verify_again')
      captchaRef.value?.reset()
      markCaptchaDirty()
      return
    }
    if (status === 429) {
      emailError.value = t('common.sending_too_frequently_please_try_again_later')
      return
    }
    emailError.value = apiErr || t('auth.verificationCodeSendFailed')
  } finally {
    emailSending.value = false
  }
}

async function handleEmailLogin() {
  emailError.value = ''
  try {
    await emailFormRef.value?.validate()
  } catch {
    return
  }
  if (needCaptcha.value && captchaConfig.value.provider !== 'custom' && !captchaToken.value) {
    emailError.value = t('auth.please_complete_the_human_verification_first')
    return
  }
  emailLoading.value = true
  try {
    const { token, user } = await emailLogin({
      email: emailForm.value.email.trim(),
      code: emailForm.value.code.trim(),
      captcha_token: captchaToken.value,
      captcha_randstr: captchaRandstr.value,
    })
    authStore.applySession(token, user)
    router.push('/chat')
  } catch (e: any) {
    const status = e.response?.status
    const apiErr = e.response?.data?.error
    if (status === 428 || apiErr === 'captcha_required') {
      needCaptcha.value = true
      emailError.value = t('auth.too_many_requests_please_complete_human_verification_and_retry')
      captchaRef.value?.reset()
      markCaptchaDirty()
      return
    }
    if (status === 403 && String(apiErr).includes('captcha')) {
      emailError.value = t('auth.human_verification_failed_please_verify_again')
      captchaRef.value?.reset()
      markCaptchaDirty()
      return
    }
    emailError.value = apiErr || t('auth.loginFailed')
  } finally {
    emailLoading.value = false
  }
}

onMounted(async () => {
  // SSO 登录回跳（successURL 带 ?sso=ok）：cookie 或 Bearer token 建立本地会话
  if (route.query.sso === 'ok' && !authStore.token) {
    const ok = await authStore.bootstrapSession()
    if (ok) {
      router.push('/chat')
      return
    }
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
  needCaptcha.value = captchaConfig.value.enabled
  try {
    const st = await getSmsStatus()
    smsEnabled.value = !!(st.enabled && st.login_enabled)
  } catch {
    // 短信服务状态不可达时隐藏短信登录入口（后端仍会兜底拒绝）
  }
  try {
    const es = await getEmailStatus()
    emailEnabled.value = !!(es.enabled && es.login_enabled)
    resetEnabled.value = !!(es.enabled && es.reset_enabled)
  } catch {
    // 邮件服务状态不可达时隐藏邮箱登录/找回密码入口（后端仍会兜底拒绝）
  }
})

async function handleLogin() {
  error.value = ''
  try {
    await formRef.value?.validate()
  } catch {
    return
  }
  if (needCaptcha.value && captchaConfig.value.provider !== 'custom' && !captchaToken.value) {
    error.value = t('auth.please_complete_the_human_verification_first')
    return
  }
  try {
    await authStore.login(form.value.email, form.value.password, {
      token: captchaToken.value,
      randstr: captchaRandstr.value,
    })
    // owner/admin 登录后进入管理后台，普通 user 进入对话
    const role = authStore.user?.role
    router.push(role === 'admin' || role === 'owner' ? '/admin' : '/chat')
  } catch (e: any) {
    const status = e.response?.status
    const apiErr = e.response?.data?.error
    if (status === 428 || apiErr === 'captcha_required') {
      // 后端要求人机验证（同 IP 失败升级）→ 强制展示验证码组件
      needCaptcha.value = true
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
    error.value = apiErr || t('auth.loginFailed')
  }
}
</script>

<template>
  <div class="login-container">
    <!-- 左侧品牌展示（≥960px 可见） -->
    <aside class="login-brand">
      <div class="login-brand-badge">
        {{ $t('agent.chiron_enterprise_grade_ai_agent_platform') }}
      </div>
      <h1 class="login-brand-title">
        {{ $t('agent.let_the_ai_agent') }}<br><span>{{ $t('common.continuous_work') }}</span>
      </h1>
      <p class="login-brand-desc">
        {{ $t('workflow.self_hosted_multi_tenant_fully_controllable_ai_agent_platform_conversation_agent_workflow_skill_knowledge_base_and_plugin_in_one') }}
      </p>
      <div class="login-brand-features">
        <div class="login-brand-feature">
          {{ $t('admin.multi_tenant_data_isolation') }}
        </div>
        <div class="login-brand-feature">
          {{ $t('common.end_to_end_trace') }}
        </div>
        <div class="login-brand-feature">
          {{ $t('agent.mcp_plugin_ecosystem') }}
        </div>
        <div class="login-brand-feature">
          {{ $t('chat.httponly_secure_session') }}
        </div>
      </div>
    </aside>

    <!-- 右侧登录表单 -->
    <div class="login-card">
      <!-- 语言切换：登录前即可选择界面语言（RTL 语言由 <html dir> 自动生效）。
           此处用内联样式：仅一处布局，不值得为它新增 scoped 规则。 -->
      <div style="display: flex; justify-content: flex-end; margin-block-end: 8px">
        <LanguageSwitcher />
      </div>
      <div class="login-header">
        <div class="login-logo">
          MC
        </div>
        <div class="login-title">
          {{ $t('common.welcome_back') }}
        </div>
        <div class="login-subtitle">
          {{ $t('auth.sign_in_to_your_ai_workspace') }}
        </div>
      </div>
      <Card
        :bordered="false"
        class="login-form-card"
      >
        <Tabs
          v-model:active-key="activeTab"
          centered
        >
          <TabPane
            key="password"
            :tab="$t('auth.password_login')"
          />

          <TabPane
            v-if="smsEnabled"
            key="sms"
            :tab="$t('auth.sms_login')"
          />

          <TabPane
            v-if="emailEnabled"
            key="email"
            :tab="$t('auth.email_login')"
          />
        </Tabs>

        <!-- 密码登录 -->
        <template v-if="activeTab === 'password'">
          <Alert
            v-if="error"
            type="error"
            :message="error"
            show-icon
            style="margin-bottom: 16px"
          />

          <Form
            ref="formRef"
            :model="form"
            :rules="rules"
            layout="vertical"
            @finish="handleLogin"
          >
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
              :label="$t('auth.password')"
              name="password"
            >
              <Input
                v-model:value="form.password"
                :placeholder="$t('auth.please_enter_password')"
                type="password"
                size="large"
                :aria-label="$t('auth.password')"
                autocomplete="current-password"
              >
                <template #prefix>
                  <LockOutlined />
                </template>
              </Input>
            </FormItem>

            <FormItem
              v-if="needCaptcha"
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
                  {{ $t('auth.login') }}
                </Button>
                <Button
                  v-if="resetEnabled"
                  type="link"
                  block
                  @click="router.push('/forgot-password')"
                >
                  {{ $t('auth.forgot_password') }}
                </Button>
                <Button
                  type="link"
                  block
                  @click="router.push('/register')"
                >
                  {{ $t('auth.no_account_register') }}
                </Button>
              </Space>
            </FormItem>
          </Form>
        </template>

        <!-- 短信登录 -->
        <template v-else-if="activeTab === 'sms'">
          <Alert
            v-if="smsError"
            type="error"
            :message="smsError"
            show-icon
            style="margin-bottom: 16px"
          />

          <Form
            ref="smsFormRef"
            :model="smsForm"
            :rules="smsRules"
            layout="vertical"
          >
            <FormItem
              :label="$t('auth.phone')"
              name="phone"
            >
              <Input
                v-model:value="smsForm.phone"
                :placeholder="$t('common.please_enter_phone_number')"
                size="large"
                :maxlength="21"
              >
                <template #prefix>
                  <MobileOutlined />
                </template>
              </Input>
            </FormItem>

            <FormItem
              :label="$t('auth.verificationCode')"
              name="code"
            >
              <Input
                v-model:value="smsForm.code"
                :placeholder="$t('auth.6_digit_verification_code')"
                size="large"
                :maxlength="6"
              >
                <template #prefix>
                  <SafetyOutlined />
                </template>
                <template #suffix>
                  <Button
                    size="small"
                    type="link"
                    :disabled="remaining > 0 || sending"
                    :loading="sending"
                    @click="handleSendCode"
                  >
                    {{ remaining > 0 ? $t('auth.resendCountdown', { s: remaining }) : $t('auth.get_verification_code') }}
                  </Button>
                </template>
              </Input>
            </FormItem>

            <FormItem
              v-if="needCaptcha"
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
              <Button
                type="primary"
                block
                size="large"
                :loading="smsLoading"
                @click="handleSmsLogin"
              >
                {{ $t('auth.login') }}
              </Button>
            </FormItem>
          </Form>
        </template>

        <!-- 邮箱验证码登录 -->
        <template v-else>
          <Alert
            v-if="emailError"
            type="error"
            :message="emailError"
            show-icon
            style="margin-bottom: 16px"
          />

          <Form
            ref="emailFormRef"
            :model="emailForm"
            :rules="emailRules"
            layout="vertical"
          >
            <FormItem
              :label="$t('auth.email')"
              name="email"
            >
              <Input
                v-model:value="emailForm.email"
                :placeholder="$t('mail.please_enter_email')"
                size="large"
                :maxlength="254"
                autocomplete="email"
              >
                <template #prefix>
                  <MailOutlined />
                </template>
              </Input>
            </FormItem>

            <FormItem
              :label="$t('auth.verificationCode')"
              name="code"
            >
              <Input
                v-model:value="emailForm.code"
                :placeholder="$t('auth.6_digit_verification_code')"
                size="large"
                :maxlength="6"
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
              v-if="needCaptcha"
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
              <Button
                type="primary"
                block
                size="large"
                :loading="emailLoading"
                @click="handleEmailLogin"
              >
                {{ $t('auth.login') }}
              </Button>
            </FormItem>
          </Form>
        </template>

        <SsoLoginButtons />
      </Card>
    </div>
  </div>
</template>

<style scoped>
.login-container {
  display: grid;
  grid-template-columns: 1fr;
  min-height: 100dvh;
  background: var(--bg-page);
  position: relative;
  overflow: hidden;
}

.login-container::before {
  content: '';
  position: absolute;
  inset: -20%;
  background:
    radial-gradient(ellipse 60% 50% at 20% 20%, var(--primary-bg), transparent 60%),
    radial-gradient(ellipse 50% 45% at 85% 15%, var(--accent-bg), transparent 60%),
    radial-gradient(ellipse 55% 50% at 50% 95%, var(--info-bg), transparent 65%);
  pointer-events: none;
  filter: blur(40px);
  opacity: 0.7;
}

.login-container::after {
  content: '';
  position: absolute;
  inset: 0;
  background-image:
    linear-gradient(var(--border-subtle) 1px, transparent 1px),
    linear-gradient(90deg, var(--border-subtle) 1px, transparent 1px);
  background-size: 32px 32px;
  mask-image: radial-gradient(ellipse 70% 70% at 50% 40%, black 15%, transparent 75%);
  -webkit-mask-image: radial-gradient(ellipse 70% 70% at 50% 40%, black 15%, transparent 75%);
  pointer-events: none;
  opacity: 0.5;
}

.login-card {
  width: 420px;
  max-width: calc(100vw - 32px);
  margin: auto;
  position: relative;
  z-index: var(--z-content);
  padding: 32px 36px;
  border-radius: var(--radius-2xl);
  background: var(--bg-surface);
  border: 1px solid var(--border-default);
  box-shadow: var(--shadow-lg), 0 0 0 1px var(--border-subtle) inset;
  animation: loginFadeIn var(--dur-slow) ease;
}

.login-header {
  text-align: center;
  margin-bottom: 28px;
}

.login-logo {
  width: 52px;
  height: 52px;
  margin: 0 auto 16px;
  border-radius: 14px;
  background: linear-gradient(135deg, var(--primary), var(--accent));
  color: var(--on-solid);
  font-weight: 700;
  font-size: 18px;
  letter-spacing: 0.02em;
  display: flex;
  align-items: center;
  justify-content: center;
  box-shadow: var(--shadow-md), 0 6px 20px var(--primary-bg);
}

.login-title {
  font-size: 24px;
  font-weight: 700;
  color: var(--text-primary);
  letter-spacing: -0.01em;
}

.login-subtitle {
  font-size: 13px;
  color: var(--text-tertiary);
  margin-top: 6px;
}

.login-form-card {
  background: transparent !important;
  border: none !important;
  box-shadow: none !important;
  padding: 0 !important;
}
.login-form-card :deep(.ant-card-body) { padding: 0; }

.login-form-card :deep(.ant-form-item-label > label) {
  font-weight: 500;
  color: var(--text-secondary);
  font-size: 13px;
  height: 24px;
}

.login-form-card :deep(.ant-input),
.login-form-card :deep(.ant-input-affix-wrapper) {
  font-size: 14px;
  transition: border-color var(--dur-fast) var(--ease-out),
              box-shadow var(--dur-fast) var(--ease-out);
}

.login-form-card :deep(.ant-btn-primary) {
  height: 44px;
  font-weight: 600;
  font-size: 14px;
  letter-spacing: 0.01em;
}

.login-form-card :deep(.ant-tabs-tab) {
  font-size: 13px;
  font-weight: 500;
}
.login-form-card :deep(.ant-tabs-tab-active .ant-tabs-tab-btn) {
  font-weight: 600;
}

@keyframes loginFadeIn {
  from { opacity: 0; transform: translateY(12px); }
  to { opacity: 1; transform: translateY(0); }
}

/* 平板以上：左右分栏品牌展示 */
@media (min-width: 960px) {
  .login-container {
    grid-template-columns: 1.1fr 1fr;
  }
  .login-brand {
    display: flex;
    flex-direction: column;
    justify-content: center;
    padding: 64px 56px;
    position: relative;
    z-index: var(--z-content);
  }
  .login-brand-badge {
    display: inline-flex;
    align-items: center;
    gap: 8px;
    padding: 5px 12px;
    border-radius: var(--radius-full);
    background: var(--primary-bg);
    color: var(--primary);
    font-size: 12px;
    font-weight: 600;
    width: fit-content;
    margin-bottom: 24px;
  }
  .login-brand-badge::before {
    content: '';
    width: 6px;
    height: 6px;
    border-radius: 50%;
    background: var(--primary);
    box-shadow: 0 0 8px var(--primary);
  }
  .login-brand-title {
    font-size: clamp(36px, 4.5vw, 52px);
    font-weight: 700;
    line-height: 1.1;
    letter-spacing: -0.02em;
    color: var(--text-primary);
    margin-bottom: 16px;
  }
  .login-brand-title span {
    background: linear-gradient(100deg, var(--primary), var(--accent));
    -webkit-background-clip: text;
    background-clip: text;
    color: transparent;
  }
  .login-brand-desc {
    font-size: 15px;
    line-height: 1.7;
    color: var(--text-secondary);
    max-width: 460px;
  }
  .login-brand-features {
    display: grid;
    grid-template-columns: repeat(2, 1fr);
    gap: 14px 28px;
    margin-top: 32px;
    max-width: 460px;
  }
  .login-brand-feature {
    display: flex;
    align-items: flex-start;
    gap: 10px;
    font-size: 13px;
    color: var(--text-secondary);
  }
  .login-brand-feature::before {
    content: '✓';
    color: var(--success);
    font-weight: 700;
    flex-shrink: 0;
  }
}

@media (max-width: 959px) {
  .login-brand { display: none; }
}

/* 移动端 */
@media (max-width: 576px) {
  .login-card {
    padding: 24px 20px;
    border-radius: var(--radius-xl);
  }
  .login-logo { width: 44px; height: 44px; font-size: 16px; }
  .login-title { font-size: 20px; }
  .login-subtitle { font-size: 12px; }
  .login-header { margin-bottom: 20px; }
  .login-form-card :deep(.ant-input) { font-size: 16px; }
  .login-form-card :deep(.ant-btn:not(.ant-btn-sm)) { min-height: 44px; }
}

/* 焦点可见性 */
.login-form-card :deep(.ant-input:focus),
.login-form-card :deep(.ant-input-affix-wrapper-focused),
.login-form-card :deep(.ant-btn:focus-visible) {
  outline: 2px solid var(--primary);
  outline-offset: 2px;
}

@media (prefers-reduced-motion: reduce) {
  .login-card { animation: none; }
}
</style>