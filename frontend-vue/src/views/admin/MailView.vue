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

async function load() {
  loading.value = true
  try {
    const cfg = await getMailAdminConfig()
    if (cfg) Object.assign(form.value, cfg)
  } catch (error: any) {
    message.error(describeApiError(error, t('加载邮件配置失败')))
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
    message.success(t('邮件配置已保存'))
  } catch (error: any) {
    message.error(describeApiError(error, t('保存邮件配置失败')))
  } finally {
    saving.value = false
  }
}

async function sendTest() {
  const to = testTo.value.trim()
  if (!isValidEmail(to)) {
    message.error(t('请输入有效的收件邮箱'))
    return
  }
  testing.value = true
  try {
    await sendMailTest(to)
    message.success(t('测试邮件已发送，请查收'))
  } catch (error: any) {
    message.error(describeApiError(error, t('测试邮件发送失败')))
  } finally {
    testing.value = false
  }
}

onMounted(load)
</script>

<template>
  <div class="mail-view">
    <Card :title="$t('邮件配置')">
      <template #extra>
        <Button
          :loading="loading"
          @click="load"
        >
          <template #icon>
            <ReloadOutlined />
          </template>
          {{ $t('刷新') }}
        </Button>
      </template>

      <Alert
        type="info"
        show-icon
        class="mail-hint"
        :message="$t('邮件用于邮箱验证码登录、注册邮箱验证、密码重置与欢迎邮件。发信服务器地址与凭据都在这里配置，保存后立即生效，无需重启服务。')"
      />

      <Form
        layout="vertical"
        class="mail-form"
      >
        <div class="switch-row">
          <FormItem :label="$t('启用邮件服务')">
            <Switch v-model:checked="form.enabled" />
          </FormItem>
        </div>
        <div class="config-note">
          {{ $t('未启用时，邮箱验证码登录 / 注册邮箱验证 / 密码重置均不可用（首页入口自动隐藏）。') }}
        </div>
      </Form>

      <Tabs v-model:activeKey="activeTab">
        <!-- ── 发信通道 ── -->
        <TabPane
          key="channel"
          :tab="$t('发信通道')"
        >
          <Form
            layout="vertical"
            class="mail-form"
          >
            <FormItem :label="$t('通道类型')">
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
                  :tab="$t('晴辰云邮 API')"
                />
              </Tabs>
            </FormItem>

            <!-- 通用 SMTP -->
            <template v-if="isSMTP">
              <FormItem :label="$t('SMTP 服务器地址')">
                <Input
                  v-model:value="form.smtp_host"
                  placeholder="smtp.example.com"
                />
              </FormItem>
              <div class="field-grid">
                <FormItem :label="$t('端口')">
                  <InputNumber
                    v-model:value="form.smtp_port"
                    :min="0"
                    :max="65535"
                    style="width: 100%"
                  />
                </FormItem>
                <FormItem :label="$t('加密方式')">
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
                      :tab="$t('不加密')"
                    />
                  </Tabs>
                </FormItem>
              </div>
              <FormItem :label="$t('SMTP 用户名')">
                <Input
                  v-model:value="form.smtp_username"
                  :placeholder="$t('多数服务商要求填写完整邮箱地址')"
                />
              </FormItem>
              <FormItem :label="$t('SMTP 口令（加密入库）')">
                <InputPassword
                  v-model:value="form.smtp_password"
                  :placeholder="passwordConfigured ? $t('已配置，留空或保持原值表示不修改') : $t('请输入口令或客户端授权码')"
                />
              </FormItem>
              <FormItem :label="$t('跳过证书校验（仅自签证书的内网服务）')">
                <Switch v-model:checked="form.smtp_skip_verify" />
              </FormItem>
              <div class="config-note">
                {{ $t('常见组合：587 + STARTTLS、465 + SSL/TLS；内网中继可用"不加密"（明文认证会记录告警日志）。') }}
              </div>
            </template>

            <!-- 晴辰云邮 HTTP API（契约见 docs/mail.md） -->
            <template v-else>
              <FormItem :label="$t('服务地址（Base URL）')">
                <Input
                  v-model:value="form.api_base_url"
                  placeholder="https://your-mail-host/api/v1"
                />
              </FormItem>
              <FormItem :label="$t('API Key（加密入库）')">
                <InputPassword
                  v-model:value="form.api_key"
                  :placeholder="apiKeyConfigured ? $t('已配置，留空或保持原值表示不修改') : 'sk_live_xxxxxxxx'"
                />
              </FormItem>
              <div class="field-grid">
                <FormItem :label="$t('发送通道 ID')">
                  <InputNumber
                    v-model:value="form.api_channel_id"
                    :min="0"
                    style="width: 100%"
                  />
                </FormItem>
                <FormItem :label="$t('模板 ID')">
                  <InputNumber
                    v-model:value="form.api_template_id"
                    :min="0"
                    style="width: 100%"
                  />
                </FormItem>
              </div>
              <div class="config-note">
                {{ $t('发送通道 ID 为 0 时由服务端自动路由；模板 ID 为 0 时直接发送下方模板渲染出的主题与正文。') }}
              </div>
            </template>
          </Form>
        </TabPane>

        <!-- ── 发件身份与站点 ── -->
        <TabPane
          key="sender"
          :tab="$t('发件身份')"
        >
          <Form
            layout="vertical"
            class="mail-form"
          >
            <FormItem :label="$t('发件地址')">
              <Input
                v-model:value="form.from_address"
                placeholder="noreply@example.com"
              />
            </FormItem>
            <FormItem :label="$t('发件人显示名')">
              <Input
                v-model:value="form.from_name"
                :placeholder="$t('如：Chiron 团队')"
              />
            </FormItem>
            <FormItem :label="$t('回复地址（可选）')">
              <Input
                v-model:value="form.reply_to"
                placeholder="support@example.com"
              />
            </FormItem>
            <FormItem :label="$t('站点名称')">
              <Input
                v-model:value="form.site_name"
                :placeholder="$t('用于邮件模板中的 {{.SiteName}}')"
              />
            </FormItem>
            <FormItem :label="$t('站点地址')">
              <Input
                v-model:value="form.app_base_url"
                placeholder="https://app.example.com"
              />
            </FormItem>
            <div class="config-note">
              {{ $t('站点地址用于拼接密码重置链接与欢迎邮件按钮；留空时回退到部署期配置的 FRONTEND_URL。') }}
            </div>
          </Form>
        </TabPane>

        <!-- ── 登录与注册能力 ── -->
        <TabPane
          key="features"
          :tab="$t('登录与注册')"
        >
          <Form
            layout="vertical"
            class="mail-form"
          >
            <FormItem :label="$t('邮箱验证码登录（免密登录）')">
              <Switch v-model:checked="form.login_enabled" />
            </FormItem>
            <FormItem :label="$t('注册必须通过邮箱验证码')">
              <Switch v-model:checked="form.register_verify" />
            </FormItem>
            <div class="config-note">
              {{ $t('开启后，注册页会出现邮箱验证码输入框，未通过校验的请求不会建号。') }}
            </div>
            <FormItem :label="$t('邮箱验证码登录时自动建号')">
              <Switch v-model:checked="form.auto_register" />
            </FormItem>
            <div class="config-note">
              {{ $t('关闭时，未注册的邮箱即使验证码正确也会被拒绝。') }}
            </div>
            <FormItem :label="$t('允许邮件找回密码')">
              <Switch v-model:checked="form.reset_enabled" />
            </FormItem>
            <FormItem :label="$t('注册成功后发送欢迎邮件')">
              <Switch v-model:checked="form.welcome_enabled" />
            </FormItem>

            <div class="field-grid">
              <FormItem :label="$t('验证码有效期（秒）')">
                <InputNumber
                  v-model:value="form.code_ttl_seconds"
                  :min="60"
                  :max="900"
                  style="width: 100%"
                />
              </FormItem>
              <FormItem :label="$t('发送间隔（秒）')">
                <InputNumber
                  v-model:value="form.send_interval_seconds"
                  :min="0"
                  :max="3600"
                  style="width: 100%"
                />
              </FormItem>
              <FormItem :label="$t('每日发送上限（每邮箱）')">
                <InputNumber
                  v-model:value="form.daily_limit"
                  :min="1"
                  :max="100"
                  style="width: 100%"
                />
              </FormItem>
              <FormItem :label="$t('发送超时（秒）')">
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
          :tab="$t('邮件模板')"
        >
          <Alert
            type="info"
            show-icon
            class="mail-hint"
            :message="$t('模板使用 Go 模板语法，可用变量：{{.SiteName}}、{{.Code}}、{{.TTLMinutes}}、{{.Action}}、{{.URL}}、{{.Email}}、{{.Name}}。留空即使用内置默认模板。')"
          />

          <Tabs size="small">
            <TabPane
              key="code"
              :tab="$t('验证码邮件')"
            >
              <Form
                layout="vertical"
                class="mail-form"
              >
                <FormItem :label="$t('主题')">
                  <Input
                    v-model:value="form.code_subject"
                    :placeholder="$t('留空使用默认：{{.SiteName}} 验证码')"
                  />
                </FormItem>
                <FormItem :label="$t('正文（HTML）')">
                  <Input.TextArea
                    v-model:value="form.code_body"
                    :rows="8"
                    :placeholder="$t('留空使用内置默认模板')"
                  />
                </FormItem>
              </Form>
            </TabPane>

            <TabPane
              key="welcome"
              :tab="$t('欢迎邮件')"
            >
              <Form
                layout="vertical"
                class="mail-form"
              >
                <FormItem :label="$t('主题')">
                  <Input
                    v-model:value="form.welcome_subject"
                    :placeholder="$t('留空使用默认：欢迎加入 {{.SiteName}}')"
                  />
                </FormItem>
                <FormItem :label="$t('正文（HTML）')">
                  <Input.TextArea
                    v-model:value="form.welcome_body"
                    :rows="8"
                    :placeholder="$t('留空使用内置默认模板')"
                  />
                </FormItem>
              </Form>
            </TabPane>

            <TabPane
              key="reset"
              :tab="$t('密码重置邮件')"
            >
              <Form
                layout="vertical"
                class="mail-form"
              >
                <FormItem :label="$t('主题')">
                  <Input
                    v-model:value="form.reset_subject"
                    :placeholder="$t('留空使用默认：{{.SiteName}} 密码重置')"
                  />
                </FormItem>
                <FormItem :label="$t('正文（HTML）')">
                  <Input.TextArea
                    v-model:value="form.reset_body"
                    :rows="8"
                    :placeholder="$t('留空使用内置默认模板（含 {{.URL}} 重置链接）')"
                  />
                </FormItem>
              </Form>
            </TabPane>
          </Tabs>
        </TabPane>

        <!-- ── 测试发信 ── -->
        <TabPane
          key="test"
          :tab="$t('测试发信')"
        >
          <Form
            layout="vertical"
            class="mail-form"
          >
            <Alert
              type="warning"
              show-icon
              class="mail-hint"
              :message="$t('测试会使用上方已保存的配置真实投递一封邮件。修改配置后请先保存，再测试。')"
            />
            <FormItem :label="$t('收件邮箱')">
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
              {{ $t('发送测试邮件') }}
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
          {{ $t('保存配置') }}
        </Button>
        <span class="config-note">
          {{ $t('保存后立即生效，多副本部署会同步到所有副本。') }}
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
