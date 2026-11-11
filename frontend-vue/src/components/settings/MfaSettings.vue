<script setup lang="ts">
/**
 * 双因子认证（MFA / TOTP）设置块。
 *
 * 后端是**两阶段启用**：`setup` 只生成密钥并落库，必须再用 `enable` 提交一次
 * 正确验证码才算启用。这个流程在 UI 上必须如实反映 —— 否则用户扫码后关掉
 * 页面，会以为自己开了双因子，实际并没有（且下次登录也不会要求验证码）。
 *
 * `pending_confirmation` 正是为这个中间态准备的：它来自后端的状态查询，
 * 本组件据此显示"未完成配置"，而不是只看 `enabled`。
 */
import { ref, onMounted } from 'vue'
import { useI18n } from 'vue-i18n'
import { message } from 'ant-design-vue'
import { SafetyOutlined, CopyOutlined } from '@ant-design/icons-vue'
import QRCode from 'qrcode'
import {
  getMfaStatus,
  setupMfa,
  enableMfa,
  disableMfa,
  type MfaStatus,
  type MfaSetupResult,
} from '../../api/auth'

const { t } = useI18n()

const loading = ref(false)
const status = ref<MfaStatus>({ enabled: false, pending_confirmation: false, recovery_codes_left: 0 })

// 启用流程的中间状态：null 表示"不在配置中"
const setup = ref<MfaSetupResult | null>(null)
const qrDataUrl = ref('')
const code = ref('')
const busy = ref(false)

// 关闭流程
const disabling = ref(false)
const disableCode = ref('')
const disablePassword = ref('')

async function refresh() {
  loading.value = true
  try {
    status.value = await getMfaStatus()
  } catch {
    // 接口失败时保持默认（未启用），不要因为读不到状态就报错打断整个设置页
    status.value = { enabled: false, pending_confirmation: false, recovery_codes_left: 0 }
  } finally {
    loading.value = false
  }
}

async function handleSetup() {
  busy.value = true
  try {
    setup.value = await setupMfa()
    // 用 otpauth:// 链接本地生成二维码，不经过任何第三方服务
    qrDataUrl.value = await QRCode.toDataURL(setup.value.otpauth_url, { margin: 1, width: 200 })
    code.value = ''
  } catch {
    message.error(t('auth.mfa_setup_failed'))
  } finally {
    busy.value = false
  }
}

async function handleEnable() {
  if (!/^\d{6}$/.test(code.value.trim())) {
    message.warning(t('auth.mfa_code_hint'))
    return
  }
  busy.value = true
  try {
    await enableMfa(code.value.trim())
    message.success(t('auth.mfa_enabled_ok'))
    setup.value = null
    qrDataUrl.value = ''
    await refresh()
  } catch {
    message.error(t('auth.mfa_code_invalid'))
  } finally {
    busy.value = false
  }
}

async function handleDisable() {
  if (!disableCode.value.trim() && !disablePassword.value.trim()) {
    message.warning(t('auth.mfa_disable_need_credential'))
    return
  }
  busy.value = true
  try {
    await disableMfa({
      code: disableCode.value.trim() || undefined,
      password: disablePassword.value.trim() || undefined,
    })
    message.success(t('auth.mfa_disabled_ok'))
    disabling.value = false
    disableCode.value = ''
    disablePassword.value = ''
    await refresh()
  } catch {
    message.error(t('auth.mfa_disable_failed'))
  } finally {
    busy.value = false
  }
}

async function copyText(text: string) {
  try {
    await navigator.clipboard.writeText(text)
    message.success(t('common.copied'))
  } catch {
    // 剪贴板不可用（非 HTTPS / 权限拒绝）时不报错：用户可手动选中复制
    message.warning(t('common.copy_manually'))
  }
}

onMounted(refresh)
</script>

<template>
  <div class="setting-block">
    <div class="setting-title">
      {{ $t('auth.mfa_title') }}
      <span class="setting-hint">
        <SafetyOutlined /> {{ $t('auth.mfa_hint') }}
      </span>
    </div>

    <a-spin :spinning="loading">
      <!-- 已启用 -->
      <div v-if="status.enabled && !disabling">
        <a-alert
          type="success"
          show-icon
          :message="$t('auth.mfa_status_enabled')"
          :description="$t('auth.mfa_recovery_left', { n: status.recovery_codes_left })"
        />
        <a-space class="mfa-actions">
          <a-button @click="disabling = true">
            {{ $t('auth.mfa_disable') }}
          </a-button>
        </a-space>
      </div>

      <!-- 关闭确认 -->
      <div v-else-if="disabling">
        <a-alert
          type="warning"
          show-icon
          :message="$t('auth.mfa_disable_confirm')"
        />
        <a-form
          layout="vertical"
          class="setting-form"
        >
          <a-form-item :label="$t('auth.mfa_code_or_password')">
            <a-input
              v-model:value="disableCode"
              :placeholder="$t('auth.mfa_code_hint')"
              :maxlength="12"
            />
          </a-form-item>
          <a-form-item :label="$t('auth.current_password')">
            <a-input-password
              v-model:value="disablePassword"
              :placeholder="$t('auth.mfa_password_placeholder')"
            />
          </a-form-item>
          <a-form-item>
            <a-space>
              <a-button
                danger
                :loading="busy"
                @click="handleDisable"
              >
                {{ $t('auth.mfa_disable') }}
              </a-button>
              <a-button @click="disabling = false">
                {{ $t('common.cancel') }}
              </a-button>
            </a-space>
          </a-form-item>
        </a-form>
      </div>

      <!-- 配置中（已生成密钥，待确认） -->
      <div v-else-if="setup">
        <a-alert
          v-if="setup.already_enabled"
          type="warning"
          show-icon
          :message="$t('auth.mfa_replacing')"
          class="mfa-alert"
        />
        <p class="mfa-step">
          {{ $t('auth.mfa_step_scan') }}
        </p>
        <div class="mfa-qr">
          <img
            v-if="qrDataUrl"
            :src="qrDataUrl"
            :alt="$t('auth.mfa_title')"
          >
        </div>
        <p class="mfa-step">
          {{ $t('auth.mfa_step_manual') }}
        </p>
        <a-input
          :value="setup.secret"
          readonly
        >
          <template #suffix>
            <CopyOutlined
              class="mfa-copy"
              @click="copyText(setup.secret)"
            />
          </template>
        </a-input>

        <p class="mfa-step">
          {{ $t('auth.mfa_step_recovery') }}
        </p>
        <div class="mfa-codes">
          <code
            v-for="c in setup.recovery_codes"
            :key="c"
          >{{ c }}</code>
        </div>
        <p class="mfa-hint">
          {{ $t('auth.mfa_recovery_warning') }}
        </p>

        <a-form
          layout="vertical"
          class="setting-form"
        >
          <a-form-item :label="$t('auth.mfa_step_confirm')">
            <a-input
              v-model:value="code"
              :placeholder="$t('auth.mfa_code_hint')"
              :maxlength="6"
              @press-enter="handleEnable"
            />
          </a-form-item>
          <a-form-item>
            <a-space>
              <a-button
                type="primary"
                :loading="busy"
                @click="handleEnable"
              >
                {{ $t('auth.mfa_enable') }}
              </a-button>
              <a-button @click="setup = null; qrDataUrl = ''">
                {{ $t('common.cancel') }}
              </a-button>
            </a-space>
          </a-form-item>
        </a-form>
      </div>

      <!-- 未启用 / 未完成 -->
      <div v-else>
        <a-alert
          v-if="status.pending_confirmation"
          type="warning"
          show-icon
          :message="$t('auth.mfa_pending')"
          class="mfa-alert"
        />
        <a-alert
          v-else
          type="info"
          show-icon
          :message="$t('auth.mfa_status_disabled')"
        />
        <a-space class="mfa-actions">
          <a-button
            type="primary"
            :loading="busy"
            @click="handleSetup"
          >
            {{ status.pending_confirmation ? $t('auth.mfa_restart') : $t('auth.mfa_enable') }}
          </a-button>
        </a-space>
      </div>
    </a-spin>
  </div>
</template>

<style scoped>
.mfa-actions { margin-top: 12px; }
.mfa-alert { margin-bottom: 12px; }
.mfa-step { margin: 12px 0 6px; font-size: 13px; color: var(--text-2); }
.mfa-qr { display: flex; justify-content: center; margin: 8px 0; }
.mfa-qr img { border-radius: var(--radius-sm); background: #fff; padding: 8px; }
.mfa-copy { cursor: pointer; color: var(--primary); }
.mfa-codes {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(140px, 1fr));
  gap: 6px;
  margin-top: 6px;
}
.mfa-codes code {
  font-family: var(--font-mono, monospace);
  font-size: 12px;
  padding: 4px 8px;
  border-radius: var(--radius-sm);
  background: var(--bg-muted);
}
.mfa-hint { margin-top: 6px; font-size: 12px; color: var(--text-3); }
@media (prefers-reduced-motion: reduce) {
  .mfa-copy { transition: none; }
}
</style>
