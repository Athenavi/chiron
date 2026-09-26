<script setup lang="ts">
/**
 * OAuthProvidersView - 管理端：三方登录 Provider 管理 + 人机验证配置 + 短信服务配置
 *
 * - Provider CRUD：v1/ent/sso/providers（协议 OIDC/OAuth2、模板类型、端点覆盖）
 * - 人机验证：v1/ent/captcha/config（turnstile/recaptcha/hcaptcha/tencent/custom）
 * - 短信服务：v1/ent/sms/config（aliyun/tencent/custom，验证码登录）
 */
import { computed, onMounted, reactive, ref } from 'vue'
import {
  Card, Table, Button, Modal, Form, FormItem, Input, Switch, Select, InputNumber,
  message, Popconfirm, Tag, Alert,
} from 'ant-design-vue'
import {
  listSsoProviders, createSsoProvider, updateSsoProvider, deleteSsoProvider,
  getCaptchaAdminConfig, updateCaptchaConfig,
  getSmsAdminConfig, updateSmsConfig,
  type SsoProvider,
} from '../../api/auth'

import { useI18n } from 'vue-i18n'
const { t } = useI18n()
// ── Provider 列表 ──

const providers = ref<SsoProvider[]>([])
const loading = ref(false)

async function loadProviders() {
  loading.value = true
  try {
    providers.value = await listSsoProviders()
  } catch (e: any) {
    message.error(e.response?.data?.error || t('errors.failed_to_load'))
  } finally {
    loading.value = false
  }
}

const columns = [
  { title: t('common.name'), dataIndex: 'name', key: 'name' },
  { title: t('common.display_name_2'), dataIndex: 'display_name', key: 'display_name' },
  { title: t('common.protocol'), dataIndex: 'protocol', key: 'protocol' },
  { title: t('common.type'), dataIndex: 'provider_type', key: 'provider_type' },
  { title: 'Client ID', dataIndex: 'client_id', key: 'client_id', ellipsis: true },
  { title: t('common.enable'), dataIndex: 'enabled', key: 'enabled' },
  { title: t('auth.auto_create_account'), dataIndex: 'auto_provision', key: 'auto_provision' },
  { title: t('common.sort'), dataIndex: 'sort_order', key: 'sort_order' },
  { title: t('common.action'), key: 'actions', width: 160 },
]

const providerTypes = computed(() => [
  { value: 'google', label: 'Google (OIDC)' },
  { value: 'github', label: 'GitHub (OAuth2)' },
  { value: 'wechat', label: t('chat.wechat_oauth2') },
  { value: 'dingtalk', label: t('common.dingtalk_oauth2') },
  { value: 'feishu', label: t('common.feishu_oauth2') },
  { value: 'qq', label: 'QQ (OAuth2)' },
  { value: 'custom', label: t('common.custom') },
])

// ── 新建 / 编辑表单 ──

const modalVisible = ref(false)
const editingId = ref<string | null>(null)
const saving = ref(false)

const providerForm = reactive({
  name: '',
  issuer: '',
  client_id: '',
  client_secret: '',
  scopes: '',
  enabled: true,
  auto_provision: true,
  protocol: 'oidc',
  provider_type: 'custom',
  display_name: '',
  sort_order: 100,
  auth_url: '',
  token_url: '',
  userinfo_url: '',
})

function openCreate() {
  editingId.value = null
  Object.assign(providerForm, {
    name: '', issuer: '', client_id: '', client_secret: '', scopes: '',
    enabled: true, auto_provision: true, protocol: 'oidc', provider_type: 'custom',
    display_name: '', sort_order: 100, auth_url: '', token_url: '', userinfo_url: '',
  })
  modalVisible.value = true
}

function openEdit(p: SsoProvider) {
  editingId.value = p.id
  Object.assign(providerForm, {
    name: p.name,
    issuer: p.issuer,
    client_id: p.client_id,
    client_secret: '', // 密文不回显；空 = 保留原值
    scopes: (p.scopes || []).join(' '),
    enabled: p.enabled,
    auto_provision: p.auto_provision,
    protocol: p.protocol,
    provider_type: p.provider_type,
    display_name: p.display_name,
    sort_order: p.sort_order,
    auth_url: p.auth_url,
    token_url: p.token_url,
    userinfo_url: p.userinfo_url,
  })
  modalVisible.value = true
}

async function handleSave() {
  if (!providerForm.name || !providerForm.client_id) {
    message.warning(t('errors.name_and_client_id_are_required'))
    return
  }
  if (providerForm.protocol === 'oidc' && !providerForm.issuer) {
    message.warning(t('common.oidc_protocol_requires_an_issuer'))
    return
  }
  if (!editingId.value && !providerForm.client_secret) {
    message.warning(t('errors.client_secret_is_required_for_a_new_provider'))
    return
  }
  const body: any = {
    name: providerForm.name,
    issuer: providerForm.issuer,
    client_id: providerForm.client_id,
    enabled: providerForm.enabled,
    auto_provision: providerForm.auto_provision,
    protocol: providerForm.protocol,
    provider_type: providerForm.provider_type,
    display_name: providerForm.display_name,
    sort_order: providerForm.sort_order,
    auth_url: providerForm.auth_url,
    token_url: providerForm.token_url,
    userinfo_url: providerForm.userinfo_url,
  }
  if (providerForm.client_secret) body.client_secret = providerForm.client_secret
  if (providerForm.scopes.trim()) {
    body.scopes = providerForm.scopes.trim().split(/[\s,]+/).filter(Boolean)
  }

  saving.value = true
  try {
    if (editingId.value) {
      await updateSsoProvider(editingId.value, body)
      message.success(t('common.updated'))
    } else {
      await createSsoProvider(body)
      message.success(t('common.created'))
    }
    modalVisible.value = false
    await loadProviders()
  } catch (e: any) {
    message.error(e.response?.data?.error || t('errors.save_failed'))
  } finally {
    saving.value = false
  }
}

async function handleDelete(p: SsoProvider) {
  try {
    await deleteSsoProvider(p.id)
    message.success(t('common.deleted'))
    await loadProviders()
  } catch (e: any) {
    message.error(e.response?.data?.error || t('errors.delete_failed'))
  }
}

// ── 人机验证配置 ──

const captcha = reactive({
  provider: 'turnstile',
  site_key: '',
  secret: '',
  verify_url: '',
  enabled: false,
})
const captchaLoading = ref(false)
const captchaSaving = ref(false)

const captchaProviders = [
  { value: 'turnstile', label: 'Cloudflare Turnstile' },
  { value: 'recaptcha', label: 'Google reCAPTCHA' },
  { value: 'hcaptcha', label: 'hCaptcha' },
  { value: 'tencent', label: t('common.tencent_waterproof_wall') },
  { value: 'custom', label: t('common.custom_http_endpoint') },
]

async function loadCaptcha() {
  captchaLoading.value = true
  try {
    const cfg = await getCaptchaAdminConfig()
    captcha.provider = cfg.provider || 'turnstile'
    captcha.site_key = cfg.site_key || ''
    captcha.secret = '' // 安全：后端应主动脱敏 secret 字段，空 = 保留原值
    captcha.verify_url = cfg.verify_url || ''
    captcha.enabled = !!cfg.enabled
  } catch (e: any) {
    message.error(e.response?.data?.error || t('auth.failed_to_load_verification_code_config'))
  } finally {
    captchaLoading.value = false
  }
}

async function handleSaveCaptcha() {
  if (captcha.enabled) {
    if (captcha.provider !== 'custom' && !captcha.site_key) {
      message.warning(t('errors.site_key_is_required_before_enabling'))
      return
    }
    if (captcha.provider === 'custom' && !captcha.verify_url) {
      message.warning(t('common.custom_type_requires_a_verify_endpoint_url'))
      return
    }
  }
  const body: any = {
    provider: captcha.provider,
    site_key: captcha.site_key,
    enabled: captcha.enabled,
    verify_url: captcha.verify_url,
  }
  if (captcha.secret) body.secret = captcha.secret

  captchaSaving.value = true
  try {
    await updateCaptchaConfig(body)
    message.success(t('auth.verification_code_config_saved'))
    captcha.secret = ''
    await loadCaptcha()
  } catch (e: any) {
    message.error(e.response?.data?.error || t('errors.save_failed'))
  } finally {
    captchaSaving.value = false
  }
}

// ── 短信服务配置 ──

const sms = reactive({
  provider: 'aliyun',
  sign_name: '',
  template_id: '',
  access_key_id: '',
  secret: '',
  endpoint: '',
  code_ttl_seconds: 300,
  send_interval_seconds: 60,
  daily_limit: 10,
  login_enabled: false,
  auto_register: false,
  enabled: false,
})
const smsLoading = ref(false)
const smsSaving = ref(false)

const smsProviders = [
  { value: 'aliyun', label: t('common.aliyun_sms') },
  { value: 'tencent', label: t('common.tencent_cloud_sms') },
  { value: 'custom', label: t('common.custom_http_endpoint') },
]

async function loadSms() {
  smsLoading.value = true
  try {
    const cfg = await getSmsAdminConfig()
    sms.provider = cfg.provider || 'aliyun'
    sms.sign_name = cfg.sign_name || ''
    sms.template_id = cfg.template_id || ''
    sms.access_key_id = cfg.access_key_id || ''
    sms.secret = '' // 安全：后端应主动脱敏 secret 字段，空 = 保留原值
    sms.endpoint = cfg.endpoint || ''
    sms.code_ttl_seconds = cfg.code_ttl_seconds || 300
    sms.send_interval_seconds = cfg.send_interval_seconds ?? 60
    sms.daily_limit = cfg.daily_limit || 10
    sms.login_enabled = !!cfg.login_enabled
    sms.auto_register = !!cfg.auto_register
    sms.enabled = !!cfg.enabled
  } catch (e: any) {
    message.error(e.response?.data?.error || t('errors.failed_to_load_sms_config'))
  } finally {
    smsLoading.value = false
  }
}

async function handleSaveSms() {
  if (sms.enabled) {
    if (sms.provider !== 'custom') {
      if (!sms.sign_name) { message.warning(t('errors.sms_signature_is_required_before_enabling')); return }
      if (!sms.template_id) { message.warning(t('errors.template_id_is_required_before_enabling')); return }
    }
    if (sms.provider === 'custom' && !sms.endpoint) {
      message.warning(t('common.custom_type_requires_a_send_endpoint_url'))
      return
    }
  }
  if (sms.login_enabled && !sms.enabled) {
    message.warning(t('auth.sms_login_depends_on_sending_capability_please_also_enable_the_sms_service'))
    return
  }
  const body: any = {
    provider: sms.provider,
    sign_name: sms.sign_name,
    template_id: sms.template_id,
    access_key_id: sms.access_key_id,
    endpoint: sms.endpoint,
    code_ttl_seconds: sms.code_ttl_seconds,
    send_interval_seconds: sms.send_interval_seconds,
    daily_limit: sms.daily_limit,
    login_enabled: sms.login_enabled,
    auto_register: sms.auto_register,
    enabled: sms.enabled,
  }
  if (sms.secret) body.secret = sms.secret

  smsSaving.value = true
  try {
    await updateSmsConfig(body)
    message.success(t('common.sms_config_saved'))
    sms.secret = ''
    await loadSms()
  } catch (e: any) {
    message.error(e.response?.data?.error || t('errors.save_failed'))
  } finally {
    smsSaving.value = false
  }
}

onMounted(() => {
  loadProviders()
  loadCaptcha()
  loadSms()
})
</script>

<template>
  <div class="oauth-providers-view">
    <Card
      :title="$t('auth.third_party_login_provider')"
      :loading="loading"
    >
      <template #extra>
        <Button
          type="primary"
          @click="openCreate"
        >
          {{ $t('common.new_provider') }}
        </Button>
      </template>

      <Alert
        type="info"
        show-icon
        style="margin-bottom: 16px"
        :message="$t('chat.selecting_a_built_in_type_github_wechat_dingtalk_etc_auto_applies_the_auth_endpoint_template_leave_custom_endpoints_empty')"
      />

      <Table
        :columns="columns"
        :data-source="providers"
        row-key="id"
        :pagination="false"
        :scroll="{ x: 1000 }"
        size="middle"
      >
        <template #emptyText>
          <div class="empty-block">
            <span class="empty-icon">📭</span><span class="empty-text">{{ $t('common.no_data_yet') }}</span>
          </div>
        </template>
        <template #bodyCell="{ column, record }">
          <template v-if="column.key === 'enabled'">
            <Tag :color="record.enabled ? 'green' : 'default'">
              {{ record.enabled ? $t('common.enable') : $t('common.disable_2') }}
            </Tag>
          </template>
          <template v-else-if="column.key === 'auto_provision'">
            <Tag :color="record.auto_provision ? 'blue' : 'default'">
              {{ record.auto_provision ? $t('common.yes') : $t('common.no') }}
            </Tag>
          </template>
          <template v-else-if="column.key === 'protocol'">
            <Tag :color="record.protocol === 'oauth2' ? 'purple' : 'cyan'">
              {{ record.protocol }}
            </Tag>
          </template>
          <template v-else-if="column.key === 'actions'">
            <Button
              size="small"
              style="margin-right: 8px"
              @click="openEdit(record as SsoProvider)"
            >
              {{ $t('common.edit_2') }}
            </Button>
            <Popconfirm
              :title="$t('common.confirm_deleting_this_provider')"
              :ok-text="$t('common.delete')"
              :cancel-text="$t('common.cancel')"
              @confirm="handleDelete(record as SsoProvider)"
            >
              <Button
                size="small"
                danger
              >
                {{ $t('common.delete') }}
              </Button>
            </Popconfirm>
          </template>
        </template>
      </Table>
    </Card>

    <Card
      :title="$t('auth.human_verification_prevents_api_abuse')"
      style="margin-top: 16px"
      :loading="captchaLoading"
    >
      <Alert
        type="info"
        show-icon
        style="margin-bottom: 16px"
        :message="$t('auth.once_enabled_login_registration_must_carry_a_captcha_token_even_when_disabled_5_consecutive_failures_from_the_same_ip_auto_escalate_to_forced_captcha_and_30_failures_are_rejected_outright')"
      />
      <Form
        layout="vertical"
        style="max-width: 520px"
      >
        <FormItem :label="$t('common.verify_provider')">
          <Select
            v-model:value="captcha.provider"
            :options="captchaProviders"
          />
        </FormItem>
        <FormItem
          v-if="captcha.provider !== 'custom'"
          label="Site Key"
        >
          <Input
            v-model:value="captcha.site_key"
            :placeholder="$t('common.site_key_for_the_frontend_rendering_component')"
          />
        </FormItem>
        <FormItem :label="$t('common.secret_blank_keeps_original')">
          <Input
            v-model:value="captcha.secret"
            :placeholder="$t('auth.server_verification_key_aes_gcm_encrypted_at_rest')"
          />
        </FormItem>
        <FormItem
          v-if="captcha.provider === 'custom'"
          :label="$t('common.verify_endpoint_url')"
        >
          <Input
            v-model:value="captcha.verify_url"
            placeholder="https://your-captcha.example.com/verify"
          />
        </FormItem>
        <FormItem :label="$t('common.enable')">
          <Switch v-model:checked="captcha.enabled" />
        </FormItem>
        <FormItem>
          <Button
            type="primary"
            :loading="captchaSaving"
            @click="handleSaveCaptcha"
          >
            {{ $t('common.save_config') }}
          </Button>
        </FormItem>
      </Form>
    </Card>

    <Card
      :title="$t('auth.sms_service_captcha_login')"
      style="margin-top: 16px"
      :loading="smsLoading"
    >
      <Alert
        type="info"
        show-icon
        style="margin-bottom: 16px"
        :message="$t('auth.once_enabled_the_login_page_shows_an_sms_login_tab_and_the_profile_page_can_bind_a_phone_number_code_sending_has_cooldown_and_daily_limits_to_prevent_abuse_accesskeysecret_is_encrypted_at_rest_and_masked_on_display')"
      />
      <Form
        layout="vertical"
        style="max-width: 520px"
      >
        <FormItem :label="$t('common.sms_provider')">
          <Select
            v-model:value="sms.provider"
            :options="smsProviders"
          />
        </FormItem>
        <template v-if="sms.provider !== 'custom'">
          <FormItem :label="$t('common.sms_signature_signname')">
            <Input
              v-model:value="sms.sign_name"
              :placeholder="$t('common.e_g_chiron')"
            />
          </FormItem>
          <FormItem :label="$t('common.template_id_alibaba_cloud_templatecode_tencent_cloud_templateid')">
            <Input
              v-model:value="sms.template_id"
              :placeholder="$t('agent.e_g_sms_12345678_template_params_must_include_code')"
            />
          </FormItem>
          <FormItem :label="$t('common.accesskeyid_tencent_cloud_smssdkappid')">
            <Input v-model:value="sms.access_key_id" />
          </FormItem>
        </template>
        <FormItem
          v-if="sms.provider === 'custom'"
          :label="$t('common.send_endpoint_url')"
        >
          <Input
            v-model:value="sms.endpoint"
            placeholder="https://your-sms.example.com/send"
          />
        </FormItem>
        <FormItem :label="$t('common.accesskeysecret_blank_keeps_original')">
          <Input
            v-model:value="sms.secret"
            type="password"
            :placeholder="$t('common.aes_gcm_encrypted_storage')"
          />
        </FormItem>
        <div class="form-grid">
          <FormItem :label="$t('auth.verification_code_validity_seconds')">
            <InputNumber
              v-model:value="sms.code_ttl_seconds"
              :min="60"
              :max="900"
              style="width: 100%"
            />
          </FormItem>
          <FormItem :label="$t('common.send_cooldown_seconds')">
            <InputNumber
              v-model:value="sms.send_interval_seconds"
              :min="0"
              :max="3600"
              style="width: 100%"
            />
          </FormItem>
        </div>
        <FormItem :label="$t('common.daily_send_limit_per_phone_number')">
          <InputNumber
            v-model:value="sms.daily_limit"
            :min="1"
            :max="100"
            style="width: 100%"
          />
        </FormItem>
        <div class="switch-row">
          <FormItem :label="$t('common.enable_sms_service')">
            <Switch v-model:checked="sms.enabled" />
          </FormItem>
          <FormItem :label="$t('auth.sms_login_entry')">
            <Switch v-model:checked="sms.login_enabled" />
          </FormItem>
          <FormItem :label="$t('auth.auto_create_account_on_unregistered')">
            <Switch v-model:checked="sms.auto_register" />
          </FormItem>
        </div>
        <FormItem>
          <Button
            type="primary"
            :loading="smsSaving"
            @click="handleSaveSms"
          >
            {{ $t('common.save_config') }}
          </Button>
        </FormItem>
      </Form>
    </Card>

    <Modal
      v-model:open="modalVisible"
      :title="editingId ? $t('common.edit_provider') : $t('common.new_provider')"
      :confirm-loading="saving"
      :ok-text="$t('common.save')"
      :cancel-text="$t('common.cancel')"
      width="640px"
      @ok="handleSave"
    >
      <Form layout="vertical">
        <div class="form-grid">
          <FormItem :label="$t('common.name_unique')">
            <Input
              v-model:value="providerForm.name"
              :placeholder="$t('common.e_g_corporate_okta')"
            />
          </FormItem>
          <FormItem :label="$t('common.display_name_2')">
            <Input
              v-model:value="providerForm.display_name"
              :placeholder="$t('auth.sign_in_button_text_defaults_to_the_name')"
            />
          </FormItem>
          <FormItem :label="$t('common.protocol')">
            <Select
              v-model:value="providerForm.protocol"
              :options="[
                { value: 'oidc', label: $t('common.oidc_standard_discovery') },
                { value: 'oauth2', label: $t('common.oauth2_explicit_endpoint') },
              ]"
            />
          </FormItem>
          <FormItem :label="$t('common.type_template')">
            <Select
              v-model:value="providerForm.provider_type"
              :options="providerTypes"
            />
          </FormItem>
          <FormItem label="Client ID">
            <Input v-model:value="providerForm.client_id" />
          </FormItem>
          <FormItem :label="$t('common.client_secret_blank_keeps_original_when_editing')">
            <Input
              v-model:value="providerForm.client_secret"
              type="password"
            />
          </FormItem>
          <FormItem
            v-if="providerForm.protocol === 'oidc'"
            label="Issuer"
          >
            <Input
              v-model:value="providerForm.issuer"
              placeholder="https://accounts.google.com"
            />
          </FormItem>
          <FormItem :label="$t('common.sort_smaller_first')">
            <InputNumber
              v-model:value="providerForm.sort_order"
              :min="0"
              :max="9999"
              style="width: 100%"
            />
          </FormItem>
        </div>
        <FormItem :label="$t('common.scopes_space_separated_blank_uses_template_default')">
          <Input
            v-model:value="providerForm.scopes"
            placeholder="openid email profile"
          />
        </FormItem>
        <div class="form-grid">
          <FormItem :label="$t('common.authorization_endpoint_override_oauth2_may_be_blank_to_use_template')">
            <Input
              v-model:value="providerForm.auth_url"
              :placeholder="$t('common.blank_template_default')"
            />
          </FormItem>
          <FormItem :label="$t('common.token_endpoint_override')">
            <Input
              v-model:value="providerForm.token_url"
              :placeholder="$t('common.blank_template_default')"
            />
          </FormItem>
          <FormItem :label="$t('admin.userinfo_endpoint_override')">
            <Input
              v-model:value="providerForm.userinfo_url"
              :placeholder="$t('common.blank_template_default')"
            />
          </FormItem>
        </div>
        <div class="switch-row">
          <FormItem :label="$t('common.enable')">
            <Switch v-model:checked="providerForm.enabled" />
          </FormItem>
          <FormItem :label="$t('auth.auto_create_account_unbound_user_auto_registers_on_first_login')">
            <Switch v-model:checked="providerForm.auto_provision" />
          </FormItem>
        </div>
      </Form>
    </Modal>
  </div>
</template>

<style scoped>
.oauth-providers-view {
  max-width: 1200px;
}

.form-grid {
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: 0 16px;
}

.switch-row {
  display: flex;
  gap: 32px;
}

/* 空状态统一 */
.empty-block {
  display: flex;
  flex-direction: column;
  align-items: center;
  gap: 6px;
  padding: 28px 0;
  color: var(--text-tertiary);
}
.empty-icon { font-size: 26px; line-height: 1; opacity: 0.8; }
.empty-text { font-size: 13px; }

/* 窄屏：表单单列、开关竖排、操作列吸底、触控目标 ≥40px */
@media (max-width: 768px) {
  .form-grid {
    grid-template-columns: 1fr;
  }
  .switch-row {
    flex-direction: column;
    gap: 0;
  }
  .switch-row .ant-form-item {
    margin-bottom: 16px;
  }
  .oauth-providers-view .ant-btn:not(.ant-btn-sm) { min-height: 40px; }
  .oauth-providers-view :deep(.ant-btn-sm) { position: relative; }
  .oauth-providers-view :deep(.ant-btn-sm)::after {
    content: '';
    position: absolute;
    inset: -8px;
    border-radius: inherit;
  }
  /* 操作列固定在右缘，不挤压主体 */
  .oauth-providers-view :deep(.ant-table-thead > tr > th:last-child),
  .oauth-providers-view :deep(.ant-table-tbody > tr > td:last-child) {
    position: sticky;
    right: 0;
    background: var(--bg-card);
    z-index: var(--z-content);
    box-shadow: -8px 0 12px -8px var(--shadow-edge);
  }
  .oauth-providers-view :deep(.ant-table-thead > tr > th:last-child) { z-index: var(--z-local); }
}
</style>