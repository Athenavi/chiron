<script setup lang="ts">
import { ref, onMounted } from 'vue'
import { useRouter } from 'vue-router'
import { Card, Form, FormItem, Input, Button, Alert, Space } from 'ant-design-vue'
import { MailOutlined, LockOutlined, UserOutlined, SafetyOutlined } from '@ant-design/icons-vue'
import { useAuthStore } from '../stores/auth'
import { getCaptchaPublicConfig, getEmailStatus, sendEmailCode, isValidEmail } from '../api/auth'
import { useSmsCountdown } from '../composables/useSmsCountdown'
import CaptchaWidget from '../components/CaptchaWidget.vue'
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
  name: [{ required: true, message: '请输入姓名', trigger: 'blur' }],
  email: [
    { required: true, message: '请输入邮箱', trigger: 'blur' },
    { type: 'email', message: '邮箱格式不正确', trigger: 'blur' },
  ],
  password: [
    { required: true, message: '请输入密码', trigger: 'blur' },
    { min: 8, message: '密码至少 8 位', trigger: 'blur' },
  ],
  confirmPassword: [
    { required: true, message: '请确认密码', trigger: 'blur' },
    {
      validator: (_rule: Rule, value: string) => {
        if (value !== form.value.password) {
          return Promise.reject('两次密码输入不一致')
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
  { required: true, message: t('请输入邮箱验证码'), trigger: 'blur' },
  { len: 6, message: t('验证码为 6 位数字'), trigger: 'blur' },
]

async function handleSendEmailCode() {
  emailCodeError.value = ''
  const email = form.value.email.trim()
  if (!isValidEmail(email)) {
    emailCodeError.value = t('请先填写正确的邮箱')
    return
  }
  if (captchaRequired.value && captchaConfig.value.provider !== 'custom' && !captchaToken.value) {
    emailCodeError.value = t('请先完成人机验证')
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
      emailCodeError.value = t('操作过于频繁，请完成人机验证后重试')
      captchaRef.value?.reset()
      markCaptchaDirty()
      return
    }
    if (status === 403 && String(apiErr).includes('captcha')) {
      emailCodeError.value = t('人机验证未通过，请重新验证')
      captchaRef.value?.reset()
      markCaptchaDirty()
      return
    }
    if (status === 429) {
      emailCodeError.value = t('发送过于频繁，请稍后再试')
      return
    }
    emailCodeError.value = apiErr || t('验证码发送失败')
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
    error.value = '请先完成人机验证'
    return
  }
  if (emailVerifyRequired.value && !form.value.emailCode.trim()) {
    error.value = t('请先获取并填写邮箱验证码')
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
      error.value = '操作过于频繁，请完成人机验证后重试'
      captchaRef.value?.reset()
      markCaptchaDirty()
      return
    }
    if (status === 403 && String(apiErr).includes('captcha')) {
      error.value = '人机验证未通过，请重新验证'
      captchaRef.value?.reset()
      markCaptchaDirty()
      return
    }
    error.value = apiErr || '注册失败'
  }
}
</script>

<template>
  <div class="register-container">
    <div class="register-card">
      <div class="register-header">
        <div class="register-logo">
          MC
        </div>
        <div class="register-title">
          {{ $t('创建账号') }}
        </div>
        <div class="register-subtitle">
          {{ $t('加入 Chiron AI Agent 平台') }}
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
          :message="$t('若系统尚无任何账号，本次注册将成为系统管理员（owner）')"
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
            :label="$t('姓名')"
            name="name"
          >
            <Input
              v-model:value="form.name"
              :placeholder="$t('请输入姓名')"
              size="large"
              aria-label="姓名"
              autocomplete="name"
            >
              <template #prefix>
                <UserOutlined />
              </template>
            </Input>
          </FormItem>

          <FormItem
            :label="$t('邮箱')"
            name="email"
          >
            <Input
              v-model:value="form.email"
              :placeholder="$t('请输入邮箱')"
              size="large"
              aria-label="邮箱"
              autocomplete="email"
            >
              <template #prefix>
                <MailOutlined />
              </template>
            </Input>
          </FormItem>

          <FormItem
            v-if="emailVerifyRequired"
            :label="$t('邮箱验证码')"
            name="emailCode"
            :rules="emailCodeRules"
            :help="emailCodeError"
            :validate-status="emailCodeError ? 'error' : undefined"
          >
            <Input
              v-model:value="form.emailCode"
              :placeholder="$t('请输入邮箱收到的验证码')"
              size="large"
              :maxlength="6"
              :aria-label="$t('邮箱验证码')"
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
                  {{ emailRemaining > 0 ? $t('auth.resendCountdown', { s: emailRemaining }) : $t('获取验证码') }}
                </Button>
              </template>
            </Input>
          </FormItem>

          <FormItem
            :label="$t('密码')"
            name="password"
          >
            <Input
              v-model:value="form.password"
              :placeholder="$t('请输入密码（至少8位）')"
              type="password"
              size="large"
              aria-label="密码"
              autocomplete="new-password"
            >
              <template #prefix>
                <LockOutlined />
              </template>
            </Input>
          </FormItem>

          <FormItem
            :label="$t('确认密码')"
            name="confirmPassword"
          >
            <Input
              v-model:value="form.confirmPassword"
              :placeholder="$t('请再次输入密码')"
              type="password"
              size="large"
              aria-label="确认密码"
              autocomplete="new-password"
            >
              <template #prefix>
                <LockOutlined />
              </template>
            </Input>
          </FormItem>

          <FormItem
            v-if="captchaRequired"
            :label="$t('人机验证')"
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
                {{ $t('注册') }}
              </Button>
              <Button
                type="link"
                block
                @click="router.push('/login')"
              >
                {{ $t('已有账号？登录') }}
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
  color: #fff;
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