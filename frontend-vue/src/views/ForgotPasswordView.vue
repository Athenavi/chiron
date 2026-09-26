<script setup lang="ts">
import { ref, onMounted } from 'vue'
import { useRouter } from 'vue-router'
import { Card, Form, FormItem, Input, Button, Alert, Space } from 'ant-design-vue'
import { MailOutlined } from '@ant-design/icons-vue'
import { getCaptchaPublicConfig, requestPasswordReset, isValidEmail } from '../api/auth'
import CaptchaWidget from '../components/CaptchaWidget.vue'
import LanguageSwitcher from '../components/common/LanguageSwitcher.vue'
import type { Rule } from 'ant-design-vue/es/form'

import { useI18n } from 'vue-i18n'
const { t } = useI18n()

// 忘记密码：向后端申请重置链接。
// 后端对"邮箱是否存在"恒返回同一结果（防账号枚举），因此本页也只展示一种成功态。

const router = useRouter()

const formRef = ref()
const form = ref({ email: '' })

const rules: Record<string, Rule[]> = {
  email: [
    { required: true, message: t('请输入邮箱'), trigger: 'blur' },
    {
      validator: (_rule: Rule, value: string) =>
        !value || isValidEmail(value) ? Promise.resolve() : Promise.reject(t('邮箱格式不正确')),
      trigger: 'blur',
    },
  ],
}

const error = ref('')
const sent = ref(false)
const loading = ref(false)

// 人机验证（与登录/注册共用同一套栅栏）
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
    await requestPasswordReset({
      email: form.value.email.trim(),
      captcha_token: captchaToken.value,
      captcha_randstr: captchaRandstr.value,
    })
    sent.value = true
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
    if (status === 403 && String(apiErr).includes('captcha')) {
      error.value = t('人机验证未通过，请重新验证')
      captchaRef.value?.reset()
      markCaptchaDirty()
      return
    }
    if (status === 403) {
      error.value = apiErr || t('本实例未开放密码重置')
      return
    }
    error.value = apiErr || t('请求失败，请稍后再试')
  } finally {
    loading.value = false
  }
}
</script>

<template>
  <div class="auth-container">
  <div class="auth-card">
    <!-- 语言切换：找回密码前即可选择界面语言 -->
    <div style="display: flex; justify-content: flex-end; margin-block-end: 8px">
      <LanguageSwitcher />
    </div>
    <div class="auth-header">
        <div class="auth-logo">
          MC
        </div>
        <div class="auth-title">
          {{ $t('auth.resetPassword') }}
        </div>
        <div class="auth-subtitle">
          {{ $t('我们会向你的邮箱发送一条重置链接') }}
        </div>
      </div>
      <Card
        :bordered="false"
        class="auth-form-card"
      >
        <Alert
          v-if="sent"
          type="success"
          :message="$t('若该邮箱已注册，重置链接已发送，请查收（含垃圾邮件箱）')"
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
          v-if="!sent"
          ref="formRef"
          :model="form"
          :rules="rules"
          layout="vertical"
          @finish="handleSubmit"
        >
          <FormItem
            :label="$t('auth.email')"
            name="email"
          >
            <Input
              v-model:value="form.email"
              :placeholder="$t('请输入注册时使用的邮箱')"
              size="large"
              :maxlength="254"
              :aria-label="$t('auth.email')"
              autocomplete="email"
            >
              <template #prefix>
                <MailOutlined />
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
                {{ $t('发送重置链接') }}
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

        <Button
          v-else
          type="link"
          block
          @click="router.push('/login')"
        >
          {{ $t('返回登录') }}
        </Button>
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
