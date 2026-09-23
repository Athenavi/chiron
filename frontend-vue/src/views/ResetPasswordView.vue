<script setup lang="ts">
import { ref, onMounted, computed } from 'vue'
import { useRouter, useRoute } from 'vue-router'
import { Card, Form, FormItem, Input, Button, Alert, Space } from 'ant-design-vue'
import { LockOutlined } from '@ant-design/icons-vue'
import { getCaptchaPublicConfig, confirmPasswordReset } from '../api/auth'
import CaptchaWidget from '../components/CaptchaWidget.vue'
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
  if (pw.length < 8) return t('密码长度不能少于 8 个字符')
  if (pw.length > 128) return t('密码长度不能超过 128 个字符')
  if (!/[A-Z]/.test(pw)) return t('密码必须包含大写字母')
  if (!/[a-z]/.test(pw)) return t('密码必须包含小写字母')
  if (!/\d/.test(pw)) return t('密码必须包含数字')
  if (!/[^A-Za-z0-9]/.test(pw)) return t('密码必须包含特殊字符（如 !@#$%^&*）')
  return ''
}

const rules: Record<string, Rule[]> = {
  password: [
    { required: true, message: t('请输入新密码'), trigger: 'blur' },
    {
      validator: (_rule: Rule, value: string) => {
        const msg = passwordComplexityError(value || '')
        return msg ? Promise.reject(msg) : Promise.resolve()
      },
      trigger: 'blur',
    },
  ],
  confirmPassword: [
    { required: true, message: t('请再次输入新密码'), trigger: 'blur' },
    {
      validator: (_rule: Rule, value: string) => {
        if (value !== form.value.password) {
          return Promise.reject(t('两次密码输入不一致'))
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
    error.value = t('重置链接无效，请重新申请')
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
    error.value = t('重置链接无效，请重新申请')
    return
  }
  try {
    await formRef.value?.validate()
  } catch {
    return
  }
  if (captchaRequired.value && captchaConfig.value.provider !== 'custom' && !captchaToken.value) {
    error.value = t('请先完成人机验证')
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
      error.value = t('操作过于频繁，请完成人机验证后重试')
      captchaRef.value?.reset()
      markCaptchaDirty()
      return
    }
    if (status === 400) {
      // 令牌失效（过期 / 已用过）与密码不合规都走这里
      error.value = apiErr || t('重置链接已失效，请重新申请')
      return
    }
    error.value = apiErr || t('重置失败，请稍后再试')
  } finally {
    loading.value = false
  }
}
</script>

<template>
  <div class="auth-container">
    <div class="auth-card">
      <div class="auth-header">
        <div class="auth-logo">
          MC
        </div>
        <div class="auth-title">
          {{ $t('设置新密码') }}
        </div>
        <div class="auth-subtitle">
          {{ $t('请设置一个新的登录密码') }}
        </div>
      </div>
      <Card
        :bordered="false"
        class="auth-form-card"
      >
        <Alert
          v-if="done"
          type="success"
          :message="$t('密码已重置，即将跳转到登录页')"
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
            :label="$t('新密码')"
            name="password"
          >
            <Input
              v-model:value="form.password"
              :placeholder="$t('至少 8 位，含大小写字母、数字与特殊字符')"
              type="password"
              size="large"
              aria-label="新密码"
              autocomplete="new-password"
            >
              <template #prefix>
                <LockOutlined />
              </template>
            </Input>
          </FormItem>

          <FormItem
            :label="$t('确认新密码')"
            name="confirmPassword"
          >
            <Input
              v-model:value="form.confirmPassword"
              :placeholder="$t('请再次输入新密码')"
              type="password"
              size="large"
              aria-label="确认新密码"
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
                size="large"
                :loading="loading"
              >
                {{ $t('确认重置') }}
              </Button>
              <Button
                type="link"
                block
                @click="router.push('/login')"
              >
                {{ $t('返回登录') }}
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
  animation: authFadeIn 0.5s ease;
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
  color: #fff;
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
