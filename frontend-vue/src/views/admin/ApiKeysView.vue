<script setup lang="ts">
/**
 * API Key / 服务提供商管理。
 *
 * 「添加」入口按 Go 网关的服务提供商目录（GET /v1/admin/llm-providers）呈现：
 * 目录式选型（国际 / 国内 / 聚合网关 / 自托管）+ 自定义端点两段式，
 * 避免把提供商写成前端硬编码下拉（旧版仅 Anthropic / OpenAI / DeepSeek 三项）。
 * 目录 id 与 llm_provider_keys.provider、引擎 keyset(llm:keys:{id}) 同名。
 */
import { ref, onMounted, computed } from 'vue'
import { Card, Row, Col, Statistic, Table, Modal, Form, FormItem, Input, Tag, Button, Spin, message } from 'ant-design-vue'
import { PlusOutlined, CheckCircleOutlined, WarningOutlined, CloseCircleOutlined } from '@ant-design/icons-vue'
import {
  listApiKeys,
  addApiKey,
  updateApiKey,
  deleteApiKey,
  listLlmProviders,
  saveLlmProviderBaseURL,
  type LlmProviderPreset,
  type LlmProviderCategory,
} from '@/api/admin'

import { useI18n } from 'vue-i18n'
const { t } = useI18n()
const loading = ref(false)
const addLoading = ref(false)
const baseSaving = ref(false)
const showAddModal = ref(false)
const providersLoading = ref(false)

type AddMode = 'catalog' | 'custom'
const addMode = ref<AddMode>('catalog')

const formData = ref({
  provider: '',
  key: '',
  remark: '',
  base_url: '',
})

const apiKeys = ref<any[]>([])
const providers = ref<LlmProviderPreset[]>([])

const CATEGORY_META: { key: LlmProviderCategory; label: string; hint: string }[] = [
  { key: 'international', label: '国际厂商', hint: 'OpenAI / Anthropic / Gemini / Grok / Groq / Mistral …' },
  { key: 'china', label: '国内厂商', hint: 'DeepSeek / Kimi / 智谱 GLM / 通义千问 / 混元 …' },
  { key: 'aggregator', label: '聚合网关', hint: 'OpenRouter / 硅基流动 / One API 自建网关' },
  { key: 'self_hosted', label: '自托管与本地', hint: 'Ollama / vLLM / LM Studio / 自定义端点' },
]

const catalogGroups = computed(() =>
  CATEGORY_META.map(meta => ({
    ...meta,
    providers: providers.value.filter(p => p.category === meta.key),
  })).filter(group => group.providers.length > 0))

const providerLabels = computed(() => {
  const map = new Map<string, string>()
  for (const p of providers.value) map.set(p.id, p.label)
  return map
})

const selectedProvider = computed(() =>
  providers.value.find(p => p.id === formData.value.provider) ?? null)

const stats = computed(() => {
  const keys = apiKeys.value
  return {
    total: keys.length,
    active: keys.filter(k => k.status === 'active').length,
    rateLimited: keys.filter(k => k.status === 'rate_limited').length,
    circuitOpen: keys.filter(k => k.status === 'circuit_open').length,
  }
})

const columns = [
  { title: 'ID', dataIndex: 'id', width: 120 },
  { title: 'Provider', dataIndex: 'provider', width: 200 },
  { title: 'Key', dataIndex: 'key_preview', width: 180 },
  { title: t('状态'), dataIndex: 'status', width: 100 },
  { title: t('权重'), dataIndex: 'weight', width: 80 },
  { title: t('失败次数'), dataIndex: 'failures', width: 100 },
  { title: t('最后使用'), dataIndex: 'last_used', width: 180 },
  { title: t('备注'), dataIndex: 'remark' },
  { title: t('操作'), dataIndex: 'actions', width: 150, fixed: 'right' as const },
]

function providerLabel(id: string): string {
  return providerLabels.value.get(id) ?? id
}

async function fetchApiKeys() {
  loading.value = true
  try {
    const keys = await listApiKeys()
    apiKeys.value = keys
  } catch {
    message.error(t('获取 API Key 失败'))
  } finally {
    loading.value = false
  }
}

async function loadProviders() {
  providersLoading.value = true
  try {
    providers.value = await listLlmProviders()
  } catch {
    // 目录拉取失败不阻断密钥管理：自定义模式仍可用
    message.error(t('服务提供商目录加载失败'))
  } finally {
    providersLoading.value = false
  }
}

// ── 添加面板 ──

function resetForm() {
  formData.value = { provider: '', key: '', remark: '', base_url: '' }
}

async function openAdd() {
  resetForm()
  addMode.value = 'catalog'
  showAddModal.value = true
  if (!providers.value.length) await loadProviders()
}

function closeAdd() {
  showAddModal.value = false
  resetForm()
}

function setMode(mode: AddMode) {
  addMode.value = mode
  resetForm()
  if (mode === 'catalog' && !providers.value.length) void loadProviders()
}

function chooseProvider(p: LlmProviderPreset) {
  formData.value.provider = p.id
  // 端点预填生效值（目录默认或管理端覆盖），便于核对/改写
  formData.value.base_url = p.effective_base_url
}

const keyPlaceholder = computed(() => {
  const prefix = selectedProvider.value?.api_key_prefix
  if (prefix) return `${prefix}…`
  return selectedProvider.value && !selectedProvider.value.requires_key
    ? t('本地端点无需 Key，可留空')
    : 'sk-…'
})

/** 校验 provider id（与网关 validLLMProviderName 一致） */
const PROVIDER_ID_RE = /^[a-z0-9._-]{1,64}$/

function isValidBaseURL(value: string): boolean {
  return /^https?:\/\/[^\s]+$/i.test(value)
}

function buildBody(): { provider: string; key: string; remark: string; base_url?: string } | null {
  const provider = formData.value.provider.trim().toLowerCase()
  if (!PROVIDER_ID_RE.test(provider)) {
    message.warning(t('Provider 名称仅允许小写字母、数字与 . _ -（最长 64 字符）'))
    return null
  }

  let key = formData.value.key.trim()
  const preset = selectedProvider.value
  const requiresKey = addMode.value === 'catalog' ? (preset?.requires_key ?? true) : true
  if (!key) {
    if (requiresKey) {
      message.warning(t('请输入 API Key'))
      return null
    }
    // 本地/自托管端点无需真实 key：用占位值满足网关的必填校验
    key = 'local'
  }

  const baseUrl = formData.value.base_url.trim()
  if (baseUrl && !isValidBaseURL(baseUrl)) {
    message.warning(t('端点需为 http(s) 地址'))
    return null
  }
  // 目录里没有默认端点的提供商（自建网关、本地推理等）必须手填，否则引擎无法注册
  if (addMode.value === 'catalog' && preset && !preset.base_url && !baseUrl) {
    message.warning(t('该提供商没有默认端点，请填写 Base URL'))
    return null
  }

  const body: { provider: string; key: string; remark: string; base_url?: string } = {
    provider,
    key,
    remark: formData.value.remark.trim(),
  }
  // 与目录默认/既有覆盖一致时不写覆盖，保持目录演进可继承
  if (baseUrl && baseUrl.replace(/\/+$/, '') !== (preset?.effective_base_url ?? '').replace(/\/+$/, '')) {
    body.base_url = baseUrl
  }
  if (addMode.value === 'custom') body.base_url = baseUrl || undefined
  return body
}

async function handleAdd() {
  const body = buildBody()
  if (!body) return

  addLoading.value = true
  try {
    await addApiKey(body)
    message.success(t('API Key 已添加'))
    closeAdd()
    await Promise.all([fetchApiKeys(), loadProviders()])
  } catch (err: any) {
    const apiErr = err?.response?.data?.error
    if (typeof apiErr === 'string' && apiErr.includes('already exists')) {
      message.error(t('该 Key 已存在，可直接使用「仅保存端点」更新端点'))
    } else {
      message.error(apiErr || t('添加失败'))
    }
  } finally {
    addLoading.value = false
  }
}

/** 只为目录内 provider 保存端点覆盖（不动 Key），供已配置提供商改端点 */
async function handleSaveBaseURL() {
  const preset = selectedProvider.value
  if (!preset) return
  const baseUrl = formData.value.base_url.trim()
  if (baseUrl && !isValidBaseURL(baseUrl)) {
    message.warning(t('端点需为 http(s) 地址'))
    return
  }
  baseSaving.value = true
  try {
    await saveLlmProviderBaseURL(preset.id, baseUrl)
    message.success(baseUrl ? t('端点已保存') : t('已恢复默认端点'))
    await loadProviders()
  } catch (err: any) {
    message.error(err?.response?.data?.error || t('端点保存失败'))
  } finally {
    baseSaving.value = false
  }
}

async function handleEdit(row: any) {
  const newStatus = row.status === 'active' ? 'rate_limited' : 'active'
  try {
    await updateApiKey(row.id, { status: newStatus })
    message.success(t('状态已更新'))
    await fetchApiKeys()
  } catch (err: any) {
    message.error('更新失败: ' + (err.message || t('未知错误')))
  }
}

async function handleDelete(row: any) {
  try {
    await deleteApiKey(row.id)
    message.success(t('API Key 已删除'))
    await fetchApiKeys()
  } catch (err: any) {
    message.error('删除失败: ' + (err.message || t('未知错误')))
  }
}

onMounted(() => {
  fetchApiKeys()
  loadProviders()
})
</script>

<template>
  <div class="api-keys">
    <Spin :spinning="loading">
      <Card :title="$t('API Key 管理')">
        <template #extra>
          <Button
            type="primary"
            size="large"
            @click="openAdd"
          >
            <template #icon>
              <PlusOutlined />
            </template>
            {{ $t('添加服务提供商') }}
          </Button>
        </template>

        <!-- 统计卡片 -->
        <Row
          :gutter="16"
          style="margin-bottom: 16px"
        >
          <Col :span="6">
            <Statistic
              :title="$t('总 Key 数')"
              :value="stats.total"
            />
          </Col>
          <Col :span="6">
            <Statistic
              :title="$t('正常')"
              :value="stats.active"
            >
              <template #prefix>
                <CheckCircleOutlined class="status-ok" />
              </template>
            </Statistic>
          </Col>
          <Col :span="6">
            <Statistic
              :title="$t('限流中')"
              :value="stats.rateLimited"
            >
              <template #prefix>
                <WarningOutlined class="status-warn" />
              </template>
            </Statistic>
          </Col>
          <Col :span="6">
            <Statistic
              :title="$t('熔断')"
              :value="stats.circuitOpen"
            >
              <template #prefix>
                <CloseCircleOutlined class="status-error" />
              </template>
            </Statistic>
          </Col>
        </Row>

        <Table
          :columns="columns"
          :data-source="apiKeys"
          :pagination="false"
          :scroll="{ x: 1060 }"
        >
          <template #bodyCell="{ column, record }">
            <template v-if="column.dataIndex === 'provider'">
              <span>{{ providerLabel(record.provider) }}</span>
              <div
                v-if="providerLabel(record.provider) !== record.provider"
                class="provider-id"
              >
                {{ record.provider }}
              </div>
            </template>
            <template v-else-if="column.dataIndex === 'status'">
              <Tag :color="record.status === 'active' ? 'success' : record.status === 'rate_limited' ? 'warning' : 'error'">
                {{ record.status === 'active' ? '正常' : record.status === 'rate_limited' ? '限流中' : '熔断' }}
              </Tag>
            </template>
            <template v-else-if="column.dataIndex === 'actions'">
              <Button
                type="link"
                size="small"
                @click="handleEdit(record)"
              >
                {{ $t('编辑') }}
              </Button>
              <Button
                type="link"
                danger
                size="small"
                @click="handleDelete(record)"
              >
                {{ $t('删除') }}
              </Button>
            </template>
          </template>
        </Table>
      </Card>
    </Spin>

    <!-- 添加服务提供商 -->
    <Modal
      v-model:visible="showAddModal"
      :title="$t('添加服务提供商')"
      :footer="null"
      width="760px"
      destroy-on-close
    >
      <div class="provider-mode">
        <Button
          size="small"
          :type="addMode === 'catalog' ? 'primary' : 'default'"
          @click="setMode('catalog')"
        >
          {{ $t('服务商目录') }}
        </Button>
        <Button
          size="small"
          :type="addMode === 'custom' ? 'primary' : 'default'"
          @click="setMode('custom')"
        >
          {{ $t('自定义端点') }}
        </Button>
        <span class="provider-mode-hint">
          {{ addMode === 'catalog' ? $t('从内置目录选择提供商，端点可按需改写') : $t('手填 Provider 名称与 OpenAI 兼容 / Anthropic 端点') }}
        </span>
      </div>

      <Spin :spinning="providersLoading">
        <!-- 目录模式 -->
        <template v-if="addMode === 'catalog'">
          <div
            v-for="group in catalogGroups"
            :key="group.key"
            class="provider-group"
          >
            <div class="provider-group-head">
              <strong>{{ group.label }}</strong>
              <span class="provider-group-hint">{{ group.hint }}</span>
            </div>
            <div class="provider-cards">
              <button
                v-for="p in group.providers"
                :key="p.id"
                type="button"
                class="provider-card"
                :class="{ active: formData.provider === p.id }"
                @click="chooseProvider(p)"
              >
                <span class="provider-card-title">
                  {{ p.label }}
                  <Tag :color="p.kind === 'anthropic' ? 'purple' : 'blue'">
                    {{ p.kind === 'anthropic' ? 'Anthropic' : 'OpenAI 兼容' }}
                  </Tag>
                </span>
                <span class="provider-card-line">{{ p.vendor }}</span>
                <span class="provider-card-line endpoint">{{ p.effective_base_url || $t('需手动填写端点') }}</span>
                <span
                  v-if="p.configured"
                  class="provider-card-state"
                >
                  {{ $t('已配置') }} {{ p.key_count }} {{ $t('个 Key') }}
                </span>
              </button>
            </div>
          </div>
          <div
            v-if="!providersLoading && !catalogGroups.length"
            class="provider-empty"
          >
            {{ $t('目录不可用，请切换到「自定义端点」手动填写') }}
          </div>
        </template>

        <!-- 自定义模式 -->
        <template v-else>
          <div class="provider-custom-note">
            {{ $t('自定义 Provider 默认按 OpenAI 兼容协议接入；需要 Anthropic 协议请选择目录中的「自定义（Anthropic 协议）」。') }}
          </div>
          <Form
            :model="formData"
            layout="vertical"
            class="provider-form"
          >
            <FormItem :label="$t('Provider 名称（小写，作为密钥分区标识）')">
              <Input
                v-model:value="formData.provider"
                placeholder="my-gateway"
              />
            </FormItem>
          </Form>
        </template>

        <!-- 选中 provider 后的凭证与端点 -->
        <Form
          v-if="addMode === 'catalog' && selectedProvider"
          :model="formData"
          layout="vertical"
          class="provider-form"
        >
          <div class="provider-form-head">
            <strong>{{ selectedProvider.label }}</strong>
            <a
              v-if="selectedProvider.docs_url"
              :href="selectedProvider.docs_url"
              target="_blank"
              rel="noopener noreferrer"
            >{{ $t('获取 API Key') }}</a>
          </div>
          <FormItem :label="$t('API Key')">
            <Input
              v-model:value="formData.key"
              :placeholder="keyPlaceholder"
            />
          </FormItem>
          <FormItem label="Base URL">
            <Input
              v-model:value="formData.base_url"
              :placeholder="selectedProvider.base_url || 'https://…'"
            />
          </FormItem>
          <FormItem :label="$t('备注')">
            <Input
              v-model:value="formData.remark"
              :placeholder="$t('可选')"
            />
          </FormItem>
        </Form>

        <Form
          v-if="addMode === 'custom'"
          :model="formData"
          layout="vertical"
          class="provider-form"
        >
          <FormItem label="Base URL">
            <Input
              v-model:value="formData.base_url"
              placeholder="https://…/v1"
            />
          </FormItem>
          <FormItem :label="$t('API Key')">
            <Input
              v-model:value="formData.key"
              placeholder="sk-…"
            />
          </FormItem>
          <FormItem :label="$t('备注')">
            <Input
              v-model:value="formData.remark"
              :placeholder="$t('可选')"
            />
          </FormItem>
        </Form>
      </Spin>

      <div class="provider-actions">
        <span class="provider-actions-hint">{{ $t('Key 与端点加密入库，引擎重启后生效') }}</span>
        <Button
          v-if="addMode === 'catalog' && selectedProvider"
          :loading="baseSaving"
          :disabled="addLoading"
          @click="handleSaveBaseURL"
        >
          {{ $t('仅保存端点') }}
        </Button>
        <Button @click="closeAdd">
          {{ $t('取消') }}
        </Button>
        <Button
          type="primary"
          :loading="addLoading"
          :disabled="!formData.provider"
          @click="handleAdd"
        >
          {{ $t('添加') }}
        </Button>
      </div>
    </Modal>
  </div>
</template>

<style scoped>
.api-keys { padding: 0; }
.status-ok { color: var(--success, #22c55e); }
.status-warn { color: var(--warning, #f59e0b); }
.status-error { color: var(--error, #ef4444); }

.provider-id {
  font-size: 12px;
  color: var(--text-tertiary, #8c8c8c);
}

/* ── 添加服务提供商面板 ── */
.provider-mode {
  display: flex;
  align-items: center;
  gap: 8px;
  margin-bottom: 12px;
}
.provider-mode-hint {
  font-size: 12px;
  color: var(--text-tertiary, #8c8c8c);
}
.provider-group { margin-bottom: 14px; }
.provider-group-head {
  display: flex;
  align-items: baseline;
  gap: 8px;
  margin-bottom: 6px;
}
.provider-group-hint {
  font-size: 12px;
  color: var(--text-tertiary, #8c8c8c);
}
.provider-cards {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(220px, 1fr));
  gap: 8px;
}
.provider-card {
  display: flex;
  flex-direction: column;
  gap: 2px;
  padding: 8px 10px;
  text-align: left;
  background: var(--surface-2, rgba(127, 127, 127, 0.06));
  border: 1px solid var(--border-color, rgba(127, 127, 127, 0.24));
  border-radius: 8px;
  cursor: pointer;
  transition: border-color 0.15s ease, background 0.15s ease;
}
.provider-card:hover { border-color: var(--primary-color, #1677ff); }
.provider-card.active {
  border-color: var(--primary-color, #1677ff);
  background: var(--primary-light, rgba(22, 119, 255, 0.10));
}
.provider-card-title {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 6px;
  font-weight: 600;
}
.provider-card-line {
  font-size: 12px;
  color: var(--text-tertiary, #8c8c8c);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.provider-card-line.endpoint { font-family: var(--font-mono, monospace); }
.provider-card-state {
  font-size: 12px;
  color: var(--success, #22c55e);
}
.provider-empty {
  padding: 16px;
  text-align: center;
  color: var(--text-tertiary, #8c8c8c);
}
.provider-form { margin-top: 12px; }
.provider-custom-note {
  margin-top: 8px;
  font-size: 12px;
  line-height: 1.6;
  color: var(--text-tertiary, #8c8c8c);
}
.provider-form-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  margin-bottom: 8px;
}
.provider-actions {
  display: flex;
  align-items: center;
  justify-content: flex-end;
  gap: 8px;
  margin-top: 16px;
}
.provider-actions-hint {
  margin-right: auto;
  font-size: 12px;
  color: var(--text-tertiary, #8c8c8c);
}

/* 移动端：统计四列改两列 */
@media (max-width: 640px) {
  .api-keys :deep(.ant-col) { flex: 0 0 50%; max-width: 50%; margin-bottom: 12px; }
}

/* 窄屏：按钮提高触控高度 */
@media (max-width: 576px) {
  .api-keys :deep(.ant-btn:not(.ant-btn-sm):not(.ant-btn-link)) { min-height: 40px; }
}
</style>
