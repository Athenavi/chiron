<script setup lang="ts">
/**
 * 后台「邮件配置」：发信通道 + 登录/注册能力 + 邮件模板。
 *
 * 要点：
 *  - 发信服务器地址（SMTP host:port / 晴辰云邮 base_url）**全部在这里配置**，
 *    代码里没有任何厂商默认域名；换服务商只需改这一页，无需重新构建；
 *  - 口令与 API Key 加密入库（后端 AES-256-GCM），页面只回显掩码占位符，
 *    原样提交即表示"保持原值"，不会被掩码串覆盖；
 *  - 「发送测试邮件」用当前配置真实投递一封，让管理员在启用前确认能通。
 */
import { ref, computed, onMounted } from 'vue'
import {
  Card, Tabs, TabPane, Form, FormItem, Input, InputNumber, InputPassword,
  Switch, Button, Alert, message,
} from 'ant-design-vue'
import { ReloadOutlined, SaveOutlined, SendOutlined } from '@ant-design/icons-vue'
import { getMailAdminConfig, updateMailConfig, sendMailTest, isValidEmail } from '@/api/auth'
import { describeApiError } from '@/utils/apiError'

import { useI18n } from 'vue-i18n'
const { t } = useI18n()

/** 后端 maskedSecret：与 internal/api 的常量一致，用于识别"未修改"的密文占位符 */
const SECRET_PLACEHOLDER = '********'

const loading = ref(false)
const saving = ref(false)
const testing = ref(false)
const activeTab = ref('channel')
const testTo = ref('')

const form = ref({
  provider: 'smtp' as 'smtp' | 'qingchen',
  enabled: false,

  smtp_host: '',
  smtp_port: 587,
  smtp_username: '',
  smtp_password: '',
  smtp_security: 'starttls' as 'none' | 'starttls' | 'ssl',
  smtp_skip_verify: false,

  api_base_url: '',
  api_key: '',
  api_channel_id: 0,
  api_template_id: 0,

  from_address: '',
  from_name: '',
  reply_to: '',
  site_name: 'Chiron',
  app_base_url: '',

  login_enabled: false,
  register_verify: false,
  auto_register: false,
  reset_enabled: false,
  welcome_enabled: true,

  code_subject: '',
  code_body: '',
  welcome_subject: '',
  welcome_body: '',
  reset_subject: '',
  reset_body: '',

  code_ttl_seconds: 300,
  send_interval_seconds: 60,
  daily_limit: 10,
  timeout_seconds: 30,
})

const isSMTP = computed(() => form.value.provider === 'smtp')
const passwordConfigured = computed(() => form.value.smtp_password === SECRET_PLACEHOLDER)
const apiKeyConfigured = computed(() => form.value.api_key === SECRET_PLACEHOLDER)

/**
 * 配置类接口的错误提示：**后端原文优先**。
 *
 * describeApiError 的设计是「code / 状态码的通用文案优先于 error 原文」（面向终端用户的
 * 请求这样更友好），但保存失败往往是「哪个字段该怎么填」的具体原因（例如
 * 「…请先启用邮件服务」），被 errors.invalid_request 盖成「请求失败，请稍后重试」后
 * 管理员就无从下手。配置页是管理操作，这里优先展示后端原话。
 */
function configErrorMessage(error: any, fallback?: string): string {
  const serverMessage = error?.response?.data?.error || error?.response?.data?.message
  return serverMessage || describeApiError(error, fallback)
}

async function load() {
  loading.value = true
  try {
    const cfg = await getMailAdminConfig()
    if (cfg) Object.assign(form.value, cfg)
  } catch (error: any) {
    message.error(describeApiError(error, t('errors.failed_to_load_email_config')))
  } finally {
    loading.value = false
  }
}

async function save() {
  saving.value = true
  try {
    // 全量提交：secret 为掩码占位符时后端保留原值
    const res = await updateMailConfig({ ...form.value })
    if (res) Object.assign(form.value, res)
    message.success(t('mail.email_config_saved'))
  } catch (error: any) {
    message.error(configErrorMessage(error, t('errors.failed_to_save_email_config')))
  } finally {
    saving.value = false
  }
}

async function sendTest() {
  const to = testTo.value.trim()
  if (!isValidEmail(to)) {
    message.error(t('mail.please_enter_a_valid_recipient_email'))
    return
  }
  testing.value = true
  try {
    await sendMailTest(to)
    message.success(t('mail.test_email_sent_please_check_your_inbox'))
  } catch (error: any) {
    message.error(configErrorMessage(error, t('errors.test_email_failed_to_send')))
  } finally {
    testing.value = false
  }
}

onMounted(load)
</script>

<template>
  <div class="mail-view">
    <Card :title="$t('mail.email_config')">
      <template #extra>
        <Button
          :loading="loading"
          @click="load"
        >
          <template #icon>
            <ReloadOutlined />
          </template>
          {{ $t('common.refresh') }}
        </Button>
      </template>

      <Alert
        type="info"
        show-icon
        class="mail-hint"
        :message="$t('auth.email_is_used_for_email_code_login_registration_email_verification_password_reset_and_welcome_emails_configure_the_mail_server_address_and_credentials_here_changes_take_effect_immediately_no_restart_needed')"
      />

      <Form
        layout="vertical"
        class="mail-form"
      >
        <div class="switch-row">
          <FormItem :label="$t('mail.enable_email_service')">
            <Switch v-model:checked="form.enabled" />
          </FormItem>
        </div>
        <div class="config-note">
          {{ $t('auth.when_disabled_email_code_login_registration_email_verification_password_reset_are_all_unavailable_home_entry_auto_hidden') }}
        </div>
      </Form>

      <Tabs v-model:active-key="activeTab">
        <!-- ── 发信通道 ── -->
        <TabPane
          key="channel"
          :tab="$t('mail.mail_channel')"
        >
          <Form
            layout="vertical"
            class="mail-form"
          >
            <FormItem :label="$t('common.channel_type')">
              <Tabs
                :active-key="form.provider"
                size="small"
                @change="(k: any) => (form.provider = k as 'smtp' | 'qingchen')"
              >
                <TabPane
                  key="smtp"
                  tab="SMTP"
                />
                <TabPane
                  key="qingchen"
                  :tab="$t('mail.qingchen_cloud_mail_api')"
                />
              </Tabs>
            </FormItem>

            <!-- 通用 SMTP -->
            <template v-if="isSMTP">
              <FormItem :label="$t('mail.smtp_server_address')">
                <Input
                  v-model:value="form.smtp_host"
                  placeholder="smtp.example.com"
                />
              </FormItem>
              <div class="field-grid">
                <FormItem :label="$t('common.port')">
                  <InputNumber
                    v-model:value="form.smtp_port"
                    :min="0"
                    :max="65535"
                    style="width: 100%"
                  />
                </FormItem>
                <FormItem :label="$t('common.encryption_method')">
                  <Tabs
                    :active-key="form.smtp_security"
                    size="small"
                    @change="(k: any) => (form.smtp_security = k as 'none' | 'starttls' | 'ssl')"
                  >
                    <TabPane
                      key="starttls"
                      tab="STARTTLS"
                    />
                    <TabPane
                      key="ssl"
                      tab="SSL/TLS"
                    />
                    <TabPane
                      key="none"
                      :tab="$t('common.no_encryption')"
                    />
                  </Tabs>
                </FormItem>
              </div>
              <FormItem :label="$t('auth.smtp_username')">
                <Input
                  v-model:value="form.smtp_username"
                  :placeholder="$t('mail.most_providers_require_a_full_email_address')"
                />
              </FormItem>
              <FormItem :label="$t('auth.smtp_password_encrypted_at_rest')">
                <InputPassword
                  v-model:value="form.smtp_password"
                  :placeholder="passwordConfigured ? $t('common.configured_blank_or_unchanged_means_no_modification') : $t('auth.please_enter_a_password_or_client_authorization_code')"
                />
              </FormItem>
              <FormItem :label="$t('auth.skip_certificate_verification_internal_services_with_self_signed_certs_only')">
                <Switch v-model:checked="form.smtp_skip_verify" />
              </FormItem>
              <div class="config-note">
                {{ $t('常见组合：587 + STARTTLS、465 + SSL/TLS；内网中继可用"不加密"（明文认证会记录告警日志）。') }}
              </div>
            </template>

            <!-- 晴辰云邮 HTTP API（契约见 docs/mail.md） -->
            <template v-else>
              <FormItem :label="$t('common.service_address_base_url')">
                <Input
                  v-model:value="form.api_base_url"
                  placeholder="https://your-mail-host/api/v1"
                />
              </FormItem>
              <div class="config-note">
                {{ $t('knowledge.must_match_the_provider_s_docs_exactly_including_the_path_prefix_e_g_api_v1_at_send_time_it_is_joined_as_service_address_send') }}
              </div>
              <FormItem :label="$t('common.api_key_encrypted_at_rest')">
                <InputPassword
                  v-model:value="form.api_key"
                  :placeholder="apiKeyConfigured ? $t('common.configured_blank_or_unchanged_means_no_modification') : 'sk_live_xxxxxxxx'"
                />
              </FormItem>
              <div class="field-grid">
                <FormItem :label="$t('common.send_channel_id')">
                  <InputNumber
                    v-model:value="form.api_channel_id"
                    :min="0"
                    style="width: 100%"
                  />
                </FormItem>
                <FormItem :label="$t('common.template_id')">
                  <InputNumber
                    v-model:value="form.api_template_id"
                    :min="0"
                    style="width: 100%"
                  />
                </FormItem>
              </div>
              <div class="config-note">
                {{ $t('admin.channel_id_0_routes_automatically_on_the_server_template_id_0_sends_the_subject_and_body_rendered_from_the_template_below') }}
              </div>
            </template>
          </Form>
        </TabPane>

        <!-- ── 发件身份与站点 ── -->
        <TabPane
          key="sender"
          :tab="$t('admin.sender_identity')"
        >
          <Form
            layout="vertical"
            class="mail-form"
          >
            <FormItem :label="$t('common.from_address')">
              <Input
                v-model:value="form.from_address"
                placeholder="noreply@example.com"
              />
            </FormItem>
            <FormItem :label="$t('common.sender_display_name')">
              <Input
                v-model:value="form.from_name"
                :placeholder="$t('common.e_g_chiron_team')"
              />
            </FormItem>
            <FormItem :label="$t('chat.reply_to_address_optional')">
              <Input
                v-model:value="form.reply_to"
                placeholder="support@example.com"
              />
            </FormItem>
            <FormItem :label="$t('common.site_name')">
              <Input
                v-model:value="form.site_name"
                :placeholder="$t('用于邮件模板中的 {ph}', { ph: '{{.SiteName}}' })"
              />
            </FormItem>
            <FormItem :label="$t('common.site_url')">
              <Input
                v-model:value="form.app_base_url"
                placeholder="https://app.example.com"
              />
            </FormItem>
            <div class="config-note">
              {{ $t('auth.site_url_is_used_to_build_password_reset_links_and_welcome_email_buttons_blank_falls_back_to_the_frontend_url_configured_at_deploy_time') }}
            </div>
          </Form>
        </TabPane>

        <!-- ── 登录与注册能力 ── -->
        <TabPane
          key="features"
          :tab="$t('auth.sign_in_sign_up')"
        >
          <Alert
            v-if="!form.enabled"
            type="warning"
            show-icon
            class="mail-hint"
            :message="$t('errors.the_switches_below_all_depend_on_enable_email_service_above_the_send_channel_tab_please_turn_on_the_master_switch_and_save_first_otherwise_the_save_will_be_rejected')"
          />
          <Form
            layout="vertical"
            class="mail-form"
          >
            <FormItem :label="$t('auth.email_code_login_passwordless')">
              <Switch v-model:checked="form.login_enabled" />
            </FormItem>
            <FormItem :label="$t('auth.registration_requires_email_verification_code')">
              <Switch v-model:checked="form.register_verify" />
            </FormItem>
            <div class="config-note">
              {{ $t('auth.when_enabled_an_email_verification_code_field_appears_on_the_registration_page_requests_that_fail_validation_will_not_create_an_account') }}
            </div>
            <FormItem :label="$t('auth.auto_create_account_on_email_code_login')">
              <Switch v-model:checked="form.auto_register" />
            </FormItem>
            <div class="config-note">
              {{ $t('auth.when_off_even_a_registered_email_with_a_correct_code_is_rejected') }}
            </div>
            <FormItem :label="$t('auth.allow_email_password_recovery')">
              <Switch v-model:checked="form.reset_enabled" />
            </FormItem>
            <FormItem :label="$t('auth.send_welcome_email_after_registration')">
              <Switch v-model:checked="form.welcome_enabled" />
            </FormItem>

            <div class="field-grid">
              <FormItem :label="$t('auth.verification_code_validity_seconds')">
                <InputNumber
                  v-model:value="form.code_ttl_seconds"
                  :min="60"
                  :max="900"
                  style="width: 100%"
                />
              </FormItem>
              <FormItem :label="$t('common.send_interval_seconds')">
                <InputNumber
                  v-model:value="form.send_interval_seconds"
                  :min="0"
                  :max="3600"
                  style="width: 100%"
                />
              </FormItem>
              <FormItem :label="$t('mail.daily_send_limit_per_email')">
                <InputNumber
                  v-model:value="form.daily_limit"
                  :min="1"
                  :max="100"
                  style="width: 100%"
                />
              </FormItem>
              <FormItem :label="$t('errors.send_timeout_seconds')">
                <InputNumber
                  v-model:value="form.timeout_seconds"
                  :min="5"
                  :max="120"
                  style="width: 100%"
                />
              </FormItem>
            </div>
            <div class="config-note">
              {{ $t('除上述限流外，发码与登录同样受"人机验证 + 验证码错误次数上限"约束（与短信通道共用栅栏）。') }}
            </div>
          </Form>
        </TabPane>

        <!-- ── 邮件模板 ── -->
        <TabPane
          key="templates"
          :tab="$t('mail.email_template')"
        >
          <Alert
            type="info"
            show-icon
            class="mail-hint"
            :message="$t('模板使用 Go 模板语法，可用变量：{vars}。留空即使用内置默认模板。', { vars: '{{.SiteName}}、{{.Code}}、{{.TTLMinutes}}、{{.Action}}、{{.URL}}、{{.Email}}、{{.Name}}' })"
          />

          <Tabs size="small">
            <TabPane
              key="code"
              :tab="$t('auth.verification_code_email')"
            >
              <Form
                layout="vertical"
                class="mail-form"
              >
                <FormItem :label="$t('settings.theme')">
                  <Input
                    v-model:value="form.code_subject"
                    :placeholder="$t('留空使用默认：{ph} 验证码', { ph: '{{.SiteName}}' })"
                  />
                </FormItem>
                <FormItem :label="$t('common.body_html')">
                  <Input.TextArea
                    v-model:value="form.code_body"
                    :rows="8"
                    :placeholder="$t('common.blank_uses_the_built_in_default_template')"
                  />
                </FormItem>
              </Form>
            </TabPane>

            <TabPane
              key="welcome"
              :tab="$t('mail.welcome_email')"
            >
              <Form
                layout="vertical"
                class="mail-form"
              >
                <FormItem :label="$t('settings.theme')">
                  <Input
                    v-model:value="form.welcome_subject"
                    :placeholder="$t('留空使用默认：欢迎加入 {ph}', { ph: '{{.SiteName}}' })"
                  />
                </FormItem>
                <FormItem :label="$t('common.body_html')">
                  <Input.TextArea
                    v-model:value="form.welcome_body"
                    :rows="8"
                    :placeholder="$t('common.blank_uses_the_built_in_default_template')"
                  />
                </FormItem>
              </Form>
            </TabPane>

            <TabPane
              key="reset"
              :tab="$t('auth.password_reset_email')"
            >
              <Form
                layout="vertical"
                class="mail-form"
              >
                <FormItem :label="$t('settings.theme')">
                  <Input
                    v-model:value="form.reset_subject"
                    :placeholder="$t('留空使用默认：{ph} 密码重置', { ph: '{{.SiteName}}' })"
                  />
                </FormItem>
                <FormItem :label="$t('common.body_html')">
                  <Input.TextArea
                    v-model:value="form.reset_body"
                    :rows="8"
                    :placeholder="$t('留空使用内置默认模板（含 {ph} 重置链接）', { ph: '{{.URL}}' })"
                  />
                </FormItem>
              </Form>
            </TabPane>
          </Tabs>
        </TabPane>

        <!-- ── 测试发信 ── -->
        <TabPane
          key="test"
          :tab="$t('mail.send_test_email')"
        >
          <Form
            layout="vertical"
            class="mail-form"
          >
            <Alert
              type="warning"
              show-icon
              class="mail-hint"
              :message="$t('mail.the_test_will_send_a_real_email_using_the_saved_config_above_save_changes_before_testing')"
            />
            <FormItem :label="$t('mail.recipient_email')">
              <Input
                v-model:value="testTo"
                placeholder="you@example.com"
              />
            </FormItem>
            <Button
              type="primary"
              :loading="testing"
              @click="sendTest"
            >
              <template #icon>
                <SendOutlined />
              </template>
              {{ $t('mail.send_test_email_2') }}
            </Button>
          </Form>
        </TabPane>
      </Tabs>

      <div class="actions">
        <Button
          type="primary"
          :loading="saving"
          @click="save"
        >
          <template #icon>
            <SaveOutlined />
          </template>
          {{ $t('common.save_config') }}
        </Button>
        <span class="config-note">
          {{ $t('common.takes_effect_immediately_and_syncs_to_all_replicas_in_a_multi_replica_deployment') }}
        </span>
      </div>
    </Card>
  </div>
</template>

<style scoped>
.mail-view { padding: 0; }

.mail-hint { margin-bottom: 16px; }

.mail-form { max-width: 720px; }

.field-grid {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(200px, 1fr));
  gap: 0 16px;
}

.switch-row { margin-bottom: 0; }

.config-note {
  margin-top: 8px;
  color: var(--text-tertiary);
  font-size: 12px;
  line-height: 1.5;
}

.actions {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 12px;
  margin-top: 16px;
}
</style>
