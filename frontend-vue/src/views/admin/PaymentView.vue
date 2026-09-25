<script setup lang="ts">
/**
 * 后台「支付配置」：支付宝 / 微信支付 / PayPal 三渠道凭据管理。
 *
 * 要点：
 *  - 凭据写入 system_settings.payment，私钥/密钥/Secret 等敏感键由后端
 *    用 APP_SECRET 派生密钥 AES-256-GCM 加密落库（见 internal/settings）；
 *  - 保存时后端先校验凭据（如 RSA 私钥无法解析直接 400 且不落库），通过后
 *    热重建渠道客户端 —— 无需重启，并广播到所有副本；
 *  - 表单回填的是「当前真正生效」的值（DB 覆盖环境变量的结果），
 *    因此不会出现"页面显示一套、实际跑另一套"。
 */
import { ref, onMounted } from 'vue'
import {
  Card, Tabs, TabPane, Form, FormItem, Input, InputPassword, Switch,
  Button, Alert, Tag, message,
} from 'ant-design-vue'
import { ReloadOutlined, SaveOutlined } from '@ant-design/icons-vue'
import { getPaymentConfig, savePaymentConfig, type PaymentChannelStatus } from '@/api/admin'
import { describeApiError } from '@/utils/apiError'

import { useI18n } from 'vue-i18n'
const { t } = useI18n()

/** 渠道 Tab 顺序与后端 paymentChannels 一致 */
const CHANNEL_TABS = [
  { key: 'alipay', label: t('支付宝') },
  { key: 'wechat', label: t('微信支付') },
  { key: 'paypal', label: 'PayPal' },
]

const loading = ref(false)
const saving = ref(false)
const activeTab = ref('alipay')

/**
 * 表单模型：键与后端 paymentConfigKeys 一一对应。
 * 保存时整体提交全量字段 —— 后端以环境变量为基准做覆盖，
 * 缺项会回退到 env，因此不能只提交变动字段。
 */
const config = ref<Record<string, any>>({
  public_base_url: '',

  alipay_enabled: true,
  alipay_app_id: '',
  alipay_private_key: '',
  alipay_public_key: '',
  alipay_gateway: '',

  wechat_enabled: true,
  wechat_mch_id: '',
  wechat_app_id: '',
  wechat_api_v3_key: '',
  wechat_mch_cert_serial_no: '',
  wechat_mch_private_key: '',

  paypal_enabled: true,
  paypal_client_id: '',
  paypal_secret: '',
  paypal_sandbox: false,
})

/** 各渠道可用性（后端判定：开关 + 必填项 + 凭据可解析） */
const channels = ref<Record<string, PaymentChannelStatus>>({})
/** 需在渠道后台登记的异步通知地址 */
const callbackUrls = ref<Record<string, string>>({})

/** 用服务端返回的生效配置回填表单（只覆盖已知键，避免脏键进入表单模型） */
function mergeConfig(src: Record<string, any>) {
  for (const key of Object.keys(config.value)) {
    if (src[key] !== undefined) config.value[key] = src[key]
  }
}

function missingText(channel: string): string {
  return (channels.value[channel]?.missing ?? []).join('、')
}

async function load() {
  loading.value = true
  try {
    const res = await getPaymentConfig()
    mergeConfig(res.config ?? {})
    channels.value = res.channels ?? {}
    callbackUrls.value = res.callback_urls ?? {}
  } catch (error: any) {
    message.error(describeApiError(error, t('加载支付配置失败')))
  } finally {
    loading.value = false
  }
}

async function save() {
  saving.value = true
  try {
    const res = await savePaymentConfig(config.value)
    // 用服务端生效结果回填：被拒绝/未生效的字段能立刻看出来
    mergeConfig(res.config ?? {})
    channels.value = res.channels ?? {}
    callbackUrls.value = res.callback_urls ?? {}
    message.success(t('支付配置已保存并生效'))
  } catch (error: any) {
    message.error(describeApiError(error, t('保存支付配置失败')))
  } finally {
    saving.value = false
  }
}

onMounted(load)
</script>

<template>
  <div class="payment-view">
    <Card :title="$t('支付渠道配置')">
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
        class="payment-hint"
        :message="$t('在此配置支付宝、微信支付与 PayPal 的商户凭据。凭据加密入库，保存后立即生效，无需重启服务。')"
      />

      <Form
        layout="vertical"
        class="base-form"
      >
        <FormItem :label="$t('公网基础 URL')">
          <Input
            v-model:value="config.public_base_url"
            placeholder="https://api.example.com"
          />
        </FormItem>
      </Form>
      <div class="config-note">
        {{ $t('用于拼接支付宝/微信的异步通知地址。必须公网可达，否则支付结果无法自动到账。') }}
      </div>

      <Tabs v-model:active-key="activeTab">
        <TabPane
          v-for="channel in CHANNEL_TABS"
          :key="channel.key"
          :tab="channel.label"
        >
          <div class="channel-head">
            <Tag :color="channels[channel.key]?.enabled ? 'success' : 'default'">
              {{ channels[channel.key]?.enabled ? $t('已生效') : $t('未生效') }}
            </Tag>
            <span class="channel-currency">{{ channels[channel.key]?.currency || '' }}</span>
            <span
              v-if="missingText(channel.key)"
              class="channel-missing"
            >
              {{ $t('缺少：') }}{{ missingText(channel.key) }}
            </span>
          </div>

          <!-- 支付宝（当面付 / Native 扫码） -->
          <Form
            v-if="channel.key === 'alipay'"
            layout="vertical"
            class="channel-form"
          >
            <FormItem :label="$t('启用支付宝')">
              <Switch v-model:checked="config.alipay_enabled" />
            </FormItem>
            <FormItem label="AppID">
              <Input
                v-model:value="config.alipay_app_id"
                placeholder="2021000000000000"
              />
            </FormItem>
            <FormItem :label="$t('应用私钥（加密入库）')">
              <Input.TextArea
                v-model:value="config.alipay_private_key"
                :rows="4"
                placeholder="-----BEGIN PRIVATE KEY-----"
              />
            </FormItem>
            <FormItem :label="$t('支付宝公钥（加密入库）')">
              <Input.TextArea
                v-model:value="config.alipay_public_key"
                :rows="4"
                placeholder="-----BEGIN PUBLIC KEY-----"
              />
            </FormItem>
            <FormItem :label="$t('网关地址')">
              <Input
                v-model:value="config.alipay_gateway"
                placeholder="https://openapi.alipay.com/gateway.do"
              />
            </FormItem>
            <FormItem :label="$t('异步通知地址（在支付宝开放平台登记）')">
              <Input
                :value="callbackUrls.alipay"
                readonly
              />
            </FormItem>
            <div class="config-note">
              {{ $t('RSA2 密钥对：应用私钥与支付宝公钥均为 PEM 格式；留空则回退到环境变量 ALIPAY_*。') }}
            </div>
          </Form>

          <!-- 微信支付（APIv3 Native 扫码） -->
          <Form
            v-else-if="channel.key === 'wechat'"
            layout="vertical"
            class="channel-form"
          >
            <FormItem :label="$t('启用微信支付')">
              <Switch v-model:checked="config.wechat_enabled" />
            </FormItem>
            <FormItem :label="$t('商户号')">
              <Input
                v-model:value="config.wechat_mch_id"
                placeholder="1900000000"
              />
            </FormItem>
            <FormItem label="AppID">
              <Input
                v-model:value="config.wechat_app_id"
                placeholder="wx0000000000000000"
              />
            </FormItem>
            <FormItem :label="$t('APIv3 密钥（加密入库）')">
              <InputPassword
                v-model:value="config.wechat_api_v3_key"
                :placeholder="$t('32 位商户密钥，用于回调解密')"
              />
            </FormItem>
            <FormItem :label="$t('商户证书序列号')">
              <Input
                v-model:value="config.wechat_mch_cert_serial_no"
                placeholder="5F2A0B1C..."
              />
            </FormItem>
            <FormItem :label="$t('商户私钥（加密入库）')">
              <Input.TextArea
                v-model:value="config.wechat_mch_private_key"
                :rows="4"
                placeholder="-----BEGIN PRIVATE KEY-----"
              />
            </FormItem>
            <FormItem :label="$t('异步通知地址（在微信商户平台登记）')">
              <Input
                :value="callbackUrls.wechat"
                readonly
              />
            </FormItem>
            <div class="config-note">
              {{ $t('商户私钥为商户 API 证书私钥（PEM）；留空则回退到环境变量 WXPAY_*。') }}
            </div>
          </Form>

          <!-- PayPal（REST Orders v2） -->
          <Form
            v-else
            layout="vertical"
            class="channel-form"
          >
            <FormItem :label="$t('启用 PayPal')">
              <Switch v-model:checked="config.paypal_enabled" />
            </FormItem>
            <FormItem label="Client ID">
              <Input
                v-model:value="config.paypal_client_id"
                placeholder="AXxxxxxxxxxxxxxxxx"
              />
            </FormItem>
            <FormItem :label="$t('Secret（加密入库）')">
              <InputPassword
                v-model:value="config.paypal_secret"
                placeholder="ELxxxxxxxxxxxxxxxx"
              />
            </FormItem>
            <FormItem :label="$t('沙箱模式')">
              <Switch v-model:checked="config.paypal_sandbox" />
            </FormItem>
            <div class="config-note">
              {{ $t('沙箱模式走 sandbox.paypal.com，仅用于联调；留空则回退到环境变量 PAYPAL_*。') }}
            </div>
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
          {{ $t('保存后立即重建支付客户端，多副本部署会同步到所有副本。') }}
        </span>
      </div>

      <div class="config-note">
        {{ $t('仅启用了且凭据齐全的渠道会出现在用户端充值页。') }}
      </div>
    </Card>
  </div>
</template>

<style scoped>
.payment-view { padding: 0; }

.payment-hint { margin-bottom: 16px; }

.base-form { max-width: 640px; }

.channel-head {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 12px;
  margin-bottom: 16px;
}

.channel-currency {
  color: var(--text-tertiary);
  font-size: 12px;
}

.channel-missing {
  color: var(--text-secondary);
  font-size: 12px;
}

.channel-form { max-width: 720px; }

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
