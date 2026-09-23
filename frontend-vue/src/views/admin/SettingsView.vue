<script setup lang="ts">
import { ref, onMounted } from 'vue'
import { Card, Row, Col, Form, FormItem, InputNumber, Input, InputPassword, Select, Button, Switch, Slider, message } from 'ant-design-vue'
import { saveSettings, getSettings, listLlmProviders } from '@/api/admin'

import { useI18n } from 'vue-i18n'
const { t } = useI18n()
const saving = ref(false)
const loading = ref(false)

const rateLimitConfig = ref({
  global: 1000,
  tenant: 500,
  user: 100,
})

const agentConfig = ref({
  max_turns: 10,
  max_tokens: 4096,
  context_limit: 20,
})

const llmConfig = ref({
  provider: 'openai',
  model: 'gpt-4o',
})

const storageConfig = ref({
  backend: 'local',
  root: './workspace',
})

// 连接与密钥配置（敏感值由 APP_SECRET 派生密钥加密入库）
const redisConfig = ref({
  addr: 'localhost:6379',
  password: '',
  db: 0,
})

const postgresConfig = ref({
  dsn: '',
})

const corsConfig = ref({
  origins: '',
})

const s3Config = ref({
  endpoint: '',
  bucket: '',
  access_key: '',
  secret_key: '',
  use_ssl: false,
})

// Python AI 引擎配置（下发到引擎，api_key 等敏感值加密入库）
const pythonConfig = ref({
  llm_provider: 'openai',
  llm_model: '',
  llm_api_key: '',
  llm_base_url: '',
  embedding_model: '',
  max_turns: 10,
  queue_worker_concurrency: 10,
  cache_l1_capacity: 2048,
})

/**
 * 默认 Provider 选项来自服务提供商目录（GET /v1/admin/llm-providers），
 * 避免把提供商写死在此处；目录不可达时退回当前值，保证下拉不空。
 */
const llmProviderOptions = ref<{ label: string; value: string }[]>([])

async function loadLlmProviderOptions() {
  try {
    const providers = await listLlmProviders()
    llmProviderOptions.value = providers.map(p => ({
      label: `${p.label}（${p.id}）`,
      value: p.id,
    }))
  } catch {
    llmProviderOptions.value = []
  }
  const current = pythonConfig.value.llm_provider
  if (current && !llmProviderOptions.value.some(o => o.value === current)) {
    llmProviderOptions.value = [{ label: current, value: current }, ...llmProviderOptions.value]
  }
}

const degradationConfig = ref({
  enabled: true,
  lightThreshold: 500000,
  mediumThreshold: 700000,
  heavyThreshold: 900000,
  vipPriority: true,
})

const cacheConfig = ref({
  l1Capacity: 2048,
  l2Ttl: 3600,
  semanticThreshold: 0.95,
  prefetchEnabled: true,
})

const apiKeyConfig = ref({
  circuitBreakerThreshold: 5,
  recoveryTimeout: 60,
  weightDecay: 0.5,
  autoRecovery: true,
})

const nginxConfig = `# /etc/nginx/nginx.conf
user nginx;
worker_processes auto;
worker_rlimit_nofile 2097152;

events {
    worker_connections 1048576;
    use epoll;
    multi_accept on;
}

http {
    sendfile on;
    tcp_nopush on;
    tcp_nodelay on;
    keepalive_timeout 65;
    keepalive_requests 1000;

    client_body_buffer_size 16K;
    client_header_buffer_size 1k;
    client_max_body_size 8m;
    large_client_header_buffers 4 8k;

    client_body_timeout 12;
    client_header_timeout 12;
    send_timeout 10;

    upstream go_gateway {
        least_conn;
        server 127.0.0.1:8080;
        server 127.0.0.1:8081;
        server 127.0.0.1:8082;
        server 127.0.0.1:8083;
        keepalive 1000;
    }

    limit_req_zone $binary_remote_addr zone=api:10m rate=100r/s;
    limit_conn_zone $binary_remote_addr zone=conn:10m;

    server {
        listen 80;
        listen 443 ssl http2;

        ssl_certificate /etc/nginx/ssl/cert.pem;
        ssl_certificate_key /etc/nginx/ssl/key.pem;
        ssl_session_cache shared:SSL:10m;
        ssl_session_timeout 10m;

        limit_req zone=api burst=200 nodelay;
        limit_conn conn 100;

        location /v1/agent/stream {
            proxy_pass http://go_gateway;
            proxy_http_version 1.1;
            proxy_set_header Connection "";
            proxy_buffering off;
            proxy_cache off;
            proxy_read_timeout 86400s;
            proxy_send_timeout 86400s;
        }

        location /v1/ {
            proxy_pass http://go_gateway;
            proxy_http_version 1.1;
            proxy_set_header Connection "";
            proxy_connect_timeout 5s;
            proxy_read_timeout 30s;
            proxy_send_timeout 30s;
        }
    }
}`

const kernelConfig = `# /etc/sysctl.conf

# 文件描述符
fs.file-max = 2097152
fs.nr_open = 2097152

# TCP 连接
net.core.somaxconn = 65535
net.ipv4.tcp_max_syn_backlog = 65535
net.ipv4.tcp_tw_reuse = 1
net.ipv4.tcp_fin_timeout = 15
net.ipv4.tcp_keepalive_time = 600
net.ipv4.tcp_keepalive_intvl = 30
net.ipv4.tcp_keepalive_probes = 3

# 端口范围
net.ipv4.ip_local_port_range = 1024 65535

# 内存
net.core.rmem_max = 16777216
net.core.wmem_max = 16777216
net.ipv4.tcp_rmem = 4096 87380 16777216
net.ipv4.tcp_wmem = 4096 65536 16777216

# 应用配置
# /etc/security/limits.conf
* soft nofile 2097152
* hard nofile 2097152
* soft nproc 65535
* hard nproc 65535`

async function saveRateLimit() {
  saving.value = true
  try {
    await saveSettings('rate_limit', rateLimitConfig.value)
    message.success(t('限流配置已保存'))
  } catch (err: any) {
    message.error('保存失败: ' + (err.message || t('未知错误')))
  } finally {
    saving.value = false
  }
}

async function saveDegradation() {
  saving.value = true
  try {
    await saveSettings('degradation', degradationConfig.value)
    message.success(t('降级配置已保存'))
  } catch (err: any) {
    message.error('保存失败: ' + (err.message || t('未知错误')))
  } finally {
    saving.value = false
  }
}

async function saveCache() {
  saving.value = true
  try {
    await saveSettings('cache', cacheConfig.value)
    message.success(t('缓存配置已保存'))
  } catch (err: any) {
    message.error('保存失败: ' + (err.message || t('未知错误')))
  } finally {
    saving.value = false
  }
}

async function saveApiKey() {
  saving.value = true
  try {
    await saveSettings('api_key', apiKeyConfig.value)
    message.success(t('API Key 配置已保存'))
  } catch (err: any) {
    message.error('保存失败: ' + (err.message || t('未知错误')))
  } finally {
    saving.value = false
  }
}

async function saveAgent() {
  saving.value = true
  try {
    await saveSettings('agent', agentConfig.value)
    message.success(t('Agent 配置已保存'))
  } catch (err: any) {
    message.error('保存失败: ' + (err.message || t('未知错误')))
  } finally {
    saving.value = false
  }
}

async function saveLlm() {
  saving.value = true
  try {
    await saveSettings('llm', llmConfig.value)
    message.success(t('模型配置已保存'))
  } catch (err: any) {
    message.error('保存失败: ' + (err.message || t('未知错误')))
  } finally {
    saving.value = false
  }
}

async function saveStorage() {
  saving.value = true
  try {
    await saveSettings('storage', storageConfig.value)
    message.success(t('存储配置已保存'))
  } catch (err: any) {
    message.error('保存失败: ' + (err.message || t('未知错误')))
  } finally {
    saving.value = false
  }
}

async function saveRedis() {
  saving.value = true
  try {
    await saveSettings('redis', redisConfig.value)
    message.success(t('Redis 配置已保存并热更新连接'))
  } catch (err: any) {
    message.error('保存失败: ' + (err.message || t('未知错误')))
  } finally {
    saving.value = false
  }
}

async function savePostgres() {
  saving.value = true
  try {
    await saveSettings('postgres', postgresConfig.value)
    message.success(t('数据库配置已保存（重启后生效）'))
  } catch (err: any) {
    message.error('保存失败: ' + (err.message || t('未知错误')))
  } finally {
    saving.value = false
  }
}

async function saveCors() {
  saving.value = true
  try {
    await saveSettings('cors', corsConfig.value)
    message.success(t('CORS 配置已保存（重启后生效）'))
  } catch (err: any) {
    message.error('保存失败: ' + (err.message || t('未知错误')))
  } finally {
    saving.value = false
  }
}

async function saveS3() {
  saving.value = true
  try {
    await saveSettings('s3', s3Config.value)
    message.success(t('对象存储配置已保存'))
  } catch (err: any) {
    message.error('保存失败: ' + (err.message || t('未知错误')))
  } finally {
    saving.value = false
  }
}

async function savePython() {
  saving.value = true
  try {
    await saveSettings('python', pythonConfig.value)
    message.success(t('Python 引擎配置已保存，引擎重启后生效'))
  } catch (err: any) {
    message.error('保存失败: ' + (err.message || t('未知错误')))
  } finally {
    saving.value = false
  }
}

const copyNginx = () => {
  navigator.clipboard.writeText(nginxConfig)
  message.success(t('已复制到剪贴板'))
}

const copyKernel = () => {
  navigator.clipboard.writeText(kernelConfig)
  message.success(t('已复制到剪贴板'))
}

// 修复：加载已持久化的真实配置（不再以写死的示例默认值覆盖线上配置）。
// 按返回的 key 覆盖默认值；后端无记录时保留默认（本地为空态）。
function mergeConfig(target: { value: Record<string, any> }, saved: Record<string, any>) {
  for (const k of Object.keys(target.value)) {
    if (saved[k] !== undefined) target.value[k] = saved[k]
  }
}

onMounted(async () => {
  loading.value = true
  try {
    mergeConfig(rateLimitConfig, await getSettings('rate_limit'))
    mergeConfig(degradationConfig, await getSettings('degradation'))
    mergeConfig(cacheConfig, await getSettings('cache'))
    mergeConfig(apiKeyConfig, await getSettings('api_key'))
    mergeConfig(agentConfig, await getSettings('agent'))
    mergeConfig(llmConfig, await getSettings('llm'))
    mergeConfig(storageConfig, await getSettings('storage'))
    mergeConfig(redisConfig, await getSettings('redis'))
    mergeConfig(postgresConfig, await getSettings('postgres'))
    mergeConfig(corsConfig, await getSettings('cors'))
    mergeConfig(s3Config, await getSettings('s3'))
    mergeConfig(pythonConfig, await getSettings('python'))
  } catch {
    // 拉取失败保留默认值，不阻断页面
  } finally {
    loading.value = false
  }
  // 目录驱动的默认 Provider 选项（在配置加载之后，便于回填当前值）
  await loadLlmProviderOptions()
})
</script>

<template>
  <div class="settings">
    <Row :gutter="16">
      <!-- 限流配置 -->
      <Col
        :xs="24"
        :sm="12"
      >
        <Card :title="$t('限流配置')">
          <Form
            :model="rateLimitConfig"
            layout="vertical"
          >
            <FormItem :label="$t('全局（每分钟）')">
              <InputNumber
                v-model:value="rateLimitConfig.global"
                :min="100"
                :max="1000000"
                style="width: 100%"
              />
            </FormItem>
            <FormItem :label="$t('单租户（每分钟）')">
              <InputNumber
                v-model:value="rateLimitConfig.tenant"
                :min="10"
                :max="100000"
                style="width: 100%"
              />
            </FormItem>
            <FormItem :label="$t('单用户（每分钟）')">
              <InputNumber
                v-model:value="rateLimitConfig.user"
                :min="1"
                :max="10000"
                style="width: 100%"
              />
            </FormItem>
          </Form>
          <Button
            type="primary"
            :loading="saving"
            @click="saveRateLimit"
          >
            {{ $t('保存') }}
          </Button>
          <div class="config-note">
            {{ $t('保存后即刻生效（热更新）。') }}
          </div>
        </Card>
      </Col>

      <!-- 降级配置 -->
      <Col
        :xs="24"
        :sm="12"
      >
        <Card :title="$t('降级配置')">
          <Form
            :model="degradationConfig"
            layout="vertical"
          >
            <FormItem :label="$t('启用降级')">
              <Switch v-model:checked="degradationConfig.enabled" />
            </FormItem>
            <FormItem :label="$t('轻度过载阈值')">
              <InputNumber
                v-model:value="degradationConfig.lightThreshold"
                :min="10000"
                :max="1000000"
                style="width: 100%"
              />
            </FormItem>
            <FormItem :label="$t('中度过载阈值')">
              <InputNumber
                v-model:value="degradationConfig.mediumThreshold"
                :min="50000"
                :max="1000000"
                style="width: 100%"
              />
            </FormItem>
            <FormItem :label="$t('重度过载阈值')">
              <InputNumber
                v-model:value="degradationConfig.heavyThreshold"
                :min="100000"
                :max="1000000"
                style="width: 100%"
              />
            </FormItem>
            <FormItem :label="$t('VIP 优先')">
              <Switch v-model:checked="degradationConfig.vipPriority" />
            </FormItem>
          </Form>
          <Button
            type="primary"
            :loading="saving"
            @click="saveDegradation"
          >
            {{ $t('保存') }}
          </Button>
        </Card>
      </Col>

      <!-- 缓存配置 -->
      <Col
        :xs="24"
        :sm="12"
      >
        <Card :title="$t('缓存配置')">
          <Form
            :model="cacheConfig"
            layout="vertical"
          >
            <FormItem :label="$t('L1 容量')">
              <InputNumber
                v-model:value="cacheConfig.l1Capacity"
                :min="100"
                :max="10000"
                style="width: 100%"
              />
            </FormItem>
            <FormItem label="L2 TTL">
              <InputNumber
                v-model:value="cacheConfig.l2Ttl"
                :min="60"
                :max="86400"
                style="width: 100%"
              />
            </FormItem>
            <FormItem :label="$t('语义缓存阈值')">
              <Slider
                v-model:value="cacheConfig.semanticThreshold"
                :min="0.5"
                :max="1"
                :step="0.01"
              />
            </FormItem>
            <FormItem :label="$t('启用预取')">
              <Switch v-model:checked="cacheConfig.prefetchEnabled" />
            </FormItem>
          </Form>
          <Button
            type="primary"
            :loading="saving"
            @click="saveCache"
          >
            {{ $t('保存') }}
          </Button>
        </Card>
      </Col>

      <!-- API Key 配置 -->
      <Col
        :xs="24"
        :sm="12"
      >
        <Card :title="$t('API Key 配置')">
          <Form
            :model="apiKeyConfig"
            layout="vertical"
          >
            <FormItem :label="$t('熔断阈值')">
              <InputNumber
                v-model:value="apiKeyConfig.circuitBreakerThreshold"
                :min="1"
                :max="100"
                style="width: 100%"
              />
            </FormItem>
            <FormItem :label="$t('恢复超时')">
              <InputNumber
                v-model:value="apiKeyConfig.recoveryTimeout"
                :min="10"
                :max="3600"
                style="width: 100%"
              />
            </FormItem>
            <FormItem :label="$t('权重衰减')">
              <Slider
                v-model:value="apiKeyConfig.weightDecay"
                :min="0.1"
                :max="1"
                :step="0.1"
              />
            </FormItem>
            <FormItem :label="$t('自动恢复')">
              <Switch v-model:checked="apiKeyConfig.autoRecovery" />
            </FormItem>
          </Form>
          <Button
            type="primary"
            :loading="saving"
            @click="saveApiKey"
          >
            {{ $t('保存') }}
          </Button>
        </Card>
      </Col>
    </Row>

    <!-- 迁移自 .env 的业务配置（持久化到 DB system_settings） -->
    <Row
      :gutter="16"
      class="config-row"
    >
      <!-- Agent 配置 -->
      <Col
        :xs="24"
        :sm="12"
      >
        <Card :title="$t('Agent 配置')">
          <Form
            :model="agentConfig"
            layout="vertical"
          >
            <FormItem :label="$t('最大推理轮数')">
              <InputNumber
                v-model:value="agentConfig.max_turns"
                :min="1"
                :max="100"
                style="width: 100%"
              />
            </FormItem>
            <FormItem :label="$t('每次调用最大 Token')">
              <InputNumber
                v-model:value="agentConfig.max_tokens"
                :min="256"
                :max="32768"
                style="width: 100%"
              />
            </FormItem>
            <FormItem :label="$t('上下文消息数限制')">
              <InputNumber
                v-model:value="agentConfig.context_limit"
                :min="1"
                :max="100"
                style="width: 100%"
              />
            </FormItem>
          </Form>
          <Button
            type="primary"
            :loading="saving"
            @click="saveAgent"
          >
            {{ $t('保存') }}
          </Button>
          <div class="config-note">
            {{ $t('保存到 DB，运行时消费项重启后生效。') }}
          </div>
        </Card>
      </Col>

      <!-- LLM / 模型配置 -->
      <Col
        :xs="24"
        :sm="12"
      >
        <Card :title="$t('模型配置')">
          <Form
            :model="llmConfig"
            layout="vertical"
          >
            <FormItem label="Provider">
              <Select
                v-model:value="llmConfig.provider"
                style="width: 100%"
              >
                <Select.Option value="openai">
                  OpenAI
                </Select.Option>
                <Select.Option value="anthropic">
                  Anthropic
                </Select.Option>
                <Select.Option value="deepseek">
                  DeepSeek
                </Select.Option>
              </Select>
            </FormItem>
            <FormItem :label="$t('默认模型')">
              <Input
                v-model:value="llmConfig.model"
                placeholder="gpt-4o"
              />
            </FormItem>
          </Form>
          <Button
            type="primary"
            :loading="saving"
            @click="saveLlm"
          >
            {{ $t('保存') }}
          </Button>
          <div class="config-note">
            {{ $t('Provider/Model 已持久化到 DB，重启生效；密钥类敏感值加密入库。') }}
          </div>
        </Card>
      </Col>

      <!-- 存储配置 -->
      <Col
        :xs="24"
        :sm="12"
      >
        <Card :title="$t('存储配置')">
          <Form
            :model="storageConfig"
            layout="vertical"
          >
            <FormItem :label="$t('后端类型')">
              <Select
                v-model:value="storageConfig.backend"
                style="width: 100%"
              >
                <Select.Option value="local">
                  {{ $t('本地磁盘') }}
                </Select.Option>
                <Select.Option value="s3">
                  S3 / MinIO
                </Select.Option>
              </Select>
            </FormItem>
            <FormItem :label="$t('存储根目录')">
              <Input
                v-model:value="storageConfig.root"
                placeholder="./workspace"
              />
            </FormItem>
          </Form>
          <Button
            type="primary"
            :loading="saving"
            @click="saveStorage"
          >
            {{ $t('保存') }}
          </Button>
          <div class="config-note">
            {{ $t('保存到 DB，运行时消费项重启后生效。') }}
          </div>
        </Card>
      </Col>
    </Row>

    <!-- 连接与密钥配置（敏感值 AES-GCM 加密入库） -->
    <Row
      :gutter="16"
      class="config-row"
    >
      <!-- Redis 配置 -->
      <Col
        :xs="24"
        :sm="12"
      >
        <Card :title="$t('Redis 配置')">
          <Form
            :model="redisConfig"
            layout="vertical"
          >
            <FormItem :label="$t('地址')">
              <Input
                v-model:value="redisConfig.addr"
                placeholder="localhost:6379"
              />
            </FormItem>
            <FormItem :label="$t('密码（加密入库）')">
              <InputPassword
                v-model:value="redisConfig.password"
                :placeholder="$t('空则无密码')"
              />
            </FormItem>
            <FormItem label="DB">
              <InputNumber
                v-model:value="redisConfig.db"
                :min="0"
                :max="15"
                style="width: 100%"
              />
            </FormItem>
          </Form>
          <Button
            type="primary"
            :loading="saving"
            @click="saveRedis"
          >
            {{ $t('保存') }}
          </Button>
          <div class="config-note">
            {{ $t('保存后热更新连接；可切换 Redis 集群。') }}
          </div>
        </Card>
      </Col>

      <!-- PostgreSQL 配置 -->
      <Col
        :xs="24"
        :sm="12"
      >
        <Card :title="$t('数据库（PostgreSQL）配置')">
          <Form
            :model="postgresConfig"
            layout="vertical"
          >
            <FormItem :label="$t('DSN（加密入库）')">
              <InputPassword
                v-model:value="postgresConfig.dsn"
                placeholder="postgres://user:pass@host:5432/chiron"
              />
            </FormItem>
          </Form>
          <Button
            type="primary"
            :loading="saving"
            @click="savePostgres"
          >
            {{ $t('保存') }}
          </Button>
          <div class="config-note">
            {{ $t('保存到 DB，切换数据库集群需重启生效。') }}
          </div>
        </Card>
      </Col>

      <!-- CORS 配置 -->
      <Col
        :xs="24"
        :sm="12"
      >
        <Card :title="$t('CORS 配置')">
          <Form
            :model="corsConfig"
            layout="vertical"
          >
            <FormItem :label="$t('允许来源（逗号分隔）')">
              <Input
                v-model:value="corsConfig.origins"
                placeholder="http://localhost:5173,https://app.example.com"
              />
            </FormItem>
          </Form>
          <Button
            type="primary"
            :loading="saving"
            @click="saveCors"
          >
            {{ $t('保存') }}
          </Button>
          <div class="config-note">
            {{ $t('保存后重启生效。') }}
          </div>
        </Card>
      </Col>

      <!-- S3 / MinIO 配置 -->
      <Col
        :xs="24"
        :sm="12"
      >
        <Card :title="$t('对象存储（S3/MinIO）配置')">
          <Form
            :model="s3Config"
            layout="vertical"
          >
            <FormItem label="Endpoint">
              <Input
                v-model:value="s3Config.endpoint"
                placeholder="localhost:9000"
              />
            </FormItem>
            <FormItem label="Bucket">
              <Input
                v-model:value="s3Config.bucket"
                placeholder="chiron-media"
              />
            </FormItem>
            <FormItem label="Access Key">
              <Input v-model:value="s3Config.access_key" />
            </FormItem>
            <FormItem :label="$t('Secret Key（加密入库）')">
              <InputPassword v-model:value="s3Config.secret_key" />
            </FormItem>
            <FormItem :label="$t('启用 SSL')">
              <Switch v-model:checked="s3Config.use_ssl" />
            </FormItem>
          </Form>
          <Button
            type="primary"
            :loading="saving"
            @click="saveS3"
          >
            {{ $t('保存') }}
          </Button>
          <div class="config-note">
            {{ $t('保存后重启生效。') }}
          </div>
        </Card>
      </Col>

      <!-- Python AI 引擎配置 -->
      <Col
        :xs="24"
        :sm="24"
      >
        <Card :title="$t('Python AI 引擎配置')">
          <Row :gutter="16">
            <Col
              :xs="24"
              :sm="12"
            >
              <Form
                :model="pythonConfig"
                layout="vertical"
              >
                <FormItem label="LLM Provider">
                  <Select
                    v-model:value="pythonConfig.llm_provider"
                    :options="llmProviderOptions"
                    show-search
                    option-filter-prop="label"
                    style="width: 100%"
                  />
                </FormItem>
                <FormItem :label="$t('默认模型')">
                  <Input
                    v-model:value="pythonConfig.llm_model"
                    placeholder="deepseek-v4-flash"
                  />
                </FormItem>
                <FormItem :label="$t('LLM API Key（加密入库）')">
                  <InputPassword v-model:value="pythonConfig.llm_api_key" />
                </FormItem>
                <FormItem label="LLM Base URL">
                  <Input
                    v-model:value="pythonConfig.llm_base_url"
                    placeholder="https://api.deepseek.com"
                  />
                </FormItem>
              </Form>
            </Col>
            <Col
              :xs="24"
              :sm="12"
            >
              <Form
                :model="pythonConfig"
                layout="vertical"
              >
                <FormItem :label="$t('Embedding 模型')">
                  <Input
                    v-model:value="pythonConfig.embedding_model"
                    placeholder="text-embedding-3-small"
                  />
                </FormItem>
                <FormItem :label="$t('Agent 最大轮数')">
                  <InputNumber
                    v-model:value="pythonConfig.max_turns"
                    :min="1"
                    :max="100"
                    style="width: 100%"
                  />
                </FormItem>
                <FormItem :label="$t('队列并发数')">
                  <InputNumber
                    v-model:value="pythonConfig.queue_worker_concurrency"
                    :min="1"
                    :max="100"
                    style="width: 100%"
                  />
                </FormItem>
                <FormItem :label="$t('L1 缓存容量')">
                  <InputNumber
                    v-model:value="pythonConfig.cache_l1_capacity"
                    :min="128"
                    :max="100000"
                    style="width: 100%"
                  />
                </FormItem>
              </Form>
            </Col>
          </Row>
          <Button
            type="primary"
            :loading="saving"
            @click="savePython"
          >
            {{ $t('保存') }}
          </Button>
          <div class="config-note">
            {{ $t('引擎启动时经内部端点拉取，API Key 加密入库。') }}
          </div>
        </Card>
      </Col>
    </Row>

    <!-- Nginx 配置 -->
    <Card
      :title="$t('Nginx 调优配置')"
      class="config-card"
    >
      <template #extra>
        <Button
          type="primary"
          ghost
          @click="copyNginx"
        >
          {{ $t('复制配置') }}
        </Button>
      </template>
      <pre class="code-block">{{ nginxConfig }}</pre>
    </Card>

    <!-- 内核调优 -->
    <Card
      :title="$t('内核调优配置')"
      class="config-card"
    >
      <template #extra>
        <Button
          type="primary"
          ghost
          @click="copyKernel"
        >
          {{ $t('复制配置') }}
        </Button>
      </template>
      <pre class="code-block">{{ kernelConfig }}</pre>
    </Card>
  </div>
</template>

<style scoped>
.settings { padding: 0; }
.config-card { margin-top: 16px; }

.config-row {
  margin-top: 16px;
}

.config-note {
  margin-top: 8px;
  color: var(--text-tertiary);
  font-size: 12px;
  line-height: 1.5;
}

.code-block {
  background: var(--bg-code);
  border: 1px solid var(--border-card);
  border-radius: var(--radius-md);
  padding: 16px;
  font-family: var(--font-mono, 'JetBrains Mono', 'Cascadia Code', 'Fira Code', monospace);
  font-size: 12px;
  line-height: 1.6;
  overflow-x: auto;
  -webkit-overflow-scrolling: touch;
  white-space: pre;
  color: var(--text-code, var(--text-primary));
}

/* 移动端 */
@media (max-width: 640px) {
  .code-block { padding: 12px; font-size: 11px; }
}

/* 窄屏：按钮提高触控高度 */
@media (max-width: 576px) {
  .settings :deep(.ant-btn:not(.ant-btn-sm):not(.ant-btn-link)) { min-height: 40px; }
}

@media (prefers-reduced-motion: reduce) {
  .code-block { transition: none; }
}
</style>