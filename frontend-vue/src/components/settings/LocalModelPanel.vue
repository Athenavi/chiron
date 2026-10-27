<script setup lang="ts">
/**
 * 本地模型配置 —— 设置页「本地模型」标签页的内容（仅本地模式显示）。
 *
 * 用户在应用内自己填：模型服务（provider）、端点（baseUrl）、模型名、API key。
 *
 * 三条硬约束：
 *   1. **key 不回显**。主进程（secrets.cjs）经 safeStorage 加密后落盘，读回的配置里
 *      只有 `hasKey` 一个布尔；这里的 apiKey 输入框永远是空的，留空保存 = 保持原 key。
 *      界面上也从不出现掩码 —— 掩码的每一位都是从真 key 派生出来的信息。
 *   2. **明文 key 不落渲染进程的持久化**：不写 localStorage、不进 pinia、不拼进 URL。
 *      保存后立即把输入框清空。
 *   3. **端点不留死值**：placeholder 只是格式示例，真正的默认地址在
 *      `electron/inference-proxy.cjs`（`DEFAULT_OLLAMA_BASE` / `CHIRON_OLLAMA_URL`），
 *      这里留空 = 用默认，避免界面与主进程各写一份默认值。
 *
 * 生效时机：推理请求由主进程的 loopback 代理转发，它每次都重读 secrets，但本地 core
 * 的模型清单/代理地址是启动时握手的，所以改完要点「重启本地后端」（或重开应用）。
 */
import { computed, onMounted, ref } from 'vue'
import { Alert, Button, Input, InputPassword, Popconfirm, Select, Spin, Tag, message } from 'ant-design-vue'
import { useI18n } from 'vue-i18n'
import { api } from '../../api'
import { errorMessageText, serverErrorDetail } from '../../utils/apiError'

const { t } = useI18n()

/** secrets.cjs 里认的 provider 名（与 electron/secrets.cjs 的 PROVIDER_NAMES 一致） */
type ProviderId = 'ollama' | 'openai-compatible'

/** 单个 provider 的**脱敏**视图：没有 apiKey 字段，只有 hasKey */
interface ProviderPublicConfig {
  baseUrl: string
  model: string
  hasKey: boolean
}

interface ModelConfigSnapshot {
  active: string
  providers: Record<string, ProviderPublicConfig>
  safeStorageAvailable: boolean
}

/** 主进程三个 IPC 统一的返回形态（不抛错，message 是给人看的一句话） */
interface ModelConfigResult {
  ok: boolean
  config?: ModelConfigSnapshot | null
  reason?: string
  message?: string
}

/** `POST /v1/models/verify` 的 data 体（desktop-local/app/api/models.py） */
interface VerifyPayload {
  ok?: boolean
  model?: string
  provider?: string
  latency_ms?: number
  reply?: string
  error?: string
}

/**
 * preload 暴露的模型配置动作。全部可选：旧外壳没有这几个成员时，
 * 面板要显示"当前版本不支持"而不是崩掉。
 */
interface ModelConfigHost {
  isDesktop?: boolean
  getModelConfig?: () => Promise<ModelConfigResult>
  saveModelConfig?: (config: unknown) => Promise<ModelConfigResult>
  restartLocalBackend?: () => Promise<ModelConfigResult>
}

const host = (globalThis as { chironDesktop?: ModelConfigHost }).chironDesktop

const hostSupported = computed(
  () => Boolean(host?.isDesktop && host?.getModelConfig && host?.saveModelConfig),
)

function emptyProvider(): ProviderPublicConfig {
  return { baseUrl: '', model: '', hasKey: false }
}

const snapshot = ref<ModelConfigSnapshot>({
  active: 'ollama',
  providers: {},
  safeStorageAvailable: true,
})
const provider = ref<ProviderId>('ollama')
const baseUrl = ref('')
const model = ref('')
const apiKey = ref('')
const loading = ref(false)
const saving = ref(false)
const testing = ref(false)
const restarting = ref(false)

const providerOptions = computed(() => [
  { value: 'ollama', label: t('settings.local_model_provider_ollama') },
  { value: 'openai-compatible', label: t('settings.local_model_provider_openai') },
])

/** 当前 provider 已保存的（脱敏）配置 */
const savedEntry = computed<ProviderPublicConfig>(
  () => snapshot.value.providers[provider.value] || emptyProvider(),
)
const hasSavedKey = computed(() => savedEntry.value.hasKey)

/** 把一份脱敏快照落到界面状态上（不碰 apiKey 输入框以外的持久态） */
function applyConfig(config: ModelConfigSnapshot | null | undefined): void {
  if (!config || typeof config !== 'object') return
  const active: ProviderId = config.active === 'openai-compatible' ? 'openai-compatible' : 'ollama'
  snapshot.value = {
    active,
    providers: config.providers && typeof config.providers === 'object' ? config.providers : {},
    safeStorageAvailable: config.safeStorageAvailable !== false,
  }
  provider.value = active
  syncFieldsFromSnapshot()
}

/** 切换 provider 时把该 provider 已存的端点/模型名带出来；key 永远留空 */
function syncFieldsFromSnapshot(): void {
  const entry = snapshot.value.providers[provider.value] || emptyProvider()
  baseUrl.value = entry.baseUrl || ''
  model.value = entry.model || ''
  apiKey.value = ''
}

function onProviderChange(value: unknown): void {
  provider.value = String(value) === 'openai-compatible' ? 'openai-compatible' : 'ollama'
  syncFieldsFromSnapshot()
}

async function load(): Promise<void> {
  if (!host?.getModelConfig) return
  loading.value = true
  try {
    const res = await host.getModelConfig()
    if (!res?.ok) {
      message.error(res?.message || t('settings.local_model_load_failed'))
      return
    }
    applyConfig(res.config)
  } catch (e) {
    message.error(errorMessageText(e, t('settings.local_model_load_failed')))
  } finally {
    loading.value = false
  }
}

async function save(): Promise<void> {
  if (!host?.saveModelConfig) return
  saving.value = true
  try {
    // 只提交当前 provider 的一条；apiKey 为空串时主进程会保留已存的 key（合并语义）
    const res = await host.saveModelConfig({
      active: provider.value,
      providers: {
        [provider.value]: {
          baseUrl: baseUrl.value,
          model: model.value,
          apiKey: apiKey.value,
        },
      },
    })
    if (!res?.ok) {
      message.error(res?.message || t('settings.local_model_save_failed'))
      return
    }
    applyConfig(res.config)
    // 保存后立刻清空：明文 key 在渲染进程里活过的每一秒都算风险
    apiKey.value = ''
    message.success(t('settings.local_model_saved'))
  } catch (e) {
    message.error(errorMessageText(e, t('settings.local_model_save_failed')))
  } finally {
    saving.value = false
  }
}

/** 明确删除已保存的 key（保存语义里"空 key = 不变"，所以要删必须单独发信号） */
async function clearKey(): Promise<void> {
  if (!host?.saveModelConfig) return
  saving.value = true
  try {
    const res = await host.saveModelConfig({
      providers: { [provider.value]: { clearApiKey: true } },
    })
    if (!res?.ok) {
      message.error(res?.message || t('settings.local_model_save_failed'))
      return
    }
    applyConfig(res.config)
    message.success(t('settings.local_model_key_cleared'))
  } catch (e) {
    message.error(errorMessageText(e, t('settings.local_model_save_failed')))
  } finally {
    saving.value = false
  }
}

/**
 * 测试连接：走本地 core 的 `POST /v1/models/verify`（它经主进程代理真的发一句 ping）。
 * 用的是**已保存**的配置 —— 没保存的新参数不会生效，界面上有相应提示。
 */
async function testConnection(): Promise<void> {
  testing.value = true
  try {
    const body: { model?: string } = {}
    const trimmed = model.value.trim()
    if (trimmed) body.model = trimmed
    const resp = await api.post('/v1/models/verify', body)
    const payload = (resp.data?.data ?? resp.data ?? {}) as VerifyPayload
    if (payload.ok === false) {
      message.error(payload.error || t('settings.local_model_test_failed'))
      return
    }
    message.success(
      t('settings.local_model_test_ok', {
        model: payload.model || trimmed || '-',
        ms: Math.round(Number(payload.latency_ms) || 0),
      }),
    )
  } catch (e) {
    // core 用非 2xx + `data.data.error` 报上游失败（FastAPI 的 detail 是另一条路径）
    const failed = (e as { response?: { data?: { data?: VerifyPayload } } })?.response?.data?.data
    message.error(failed?.error || serverErrorDetail(e, t('settings.local_model_test_failed')))
  } finally {
    testing.value = false
  }
}

async function restartBackend(): Promise<void> {
  if (!host?.restartLocalBackend) return
  restarting.value = true
  try {
    const res = await host.restartLocalBackend()
    if (res?.ok) message.success(t('settings.local_model_restarted'))
    else message.error(res?.message || t('settings.local_model_restart_failed'))
  } catch (e) {
    message.error(errorMessageText(e, t('settings.local_model_restart_failed')))
  } finally {
    restarting.value = false
  }
}

onMounted(() => {
  void load()
})
</script>

<template>
  <div class="local-model-panel">
    <Alert
      v-if="!hostSupported"
      type="warning"
      show-icon
      :message="$t('settings.local_model_unsupported')"
    />
    <template v-else>
      <Alert
        type="info"
        show-icon
        class="lm-alert"
        :message="$t('settings.local_model_restart_hint')"
      />
      <Alert
        v-if="!snapshot.safeStorageAvailable"
        type="error"
        show-icon
        class="lm-alert"
        :message="$t('settings.local_model_no_safe_storage')"
      />

      <Spin :spinning="loading">
        <div class="lm-block">
          <div class="lm-row">
            <span class="lm-label">{{ $t('settings.local_model_provider') }}</span>
            <Select
              :value="provider"
              :options="providerOptions"
              style="width: 220px"
              @change="onProviderChange"
            />
            <span class="lm-hint">{{ $t('settings.local_model_provider_hint') }}</span>
          </div>

          <div class="lm-row">
            <span class="lm-label">{{ $t('settings.local_model_base_url') }}</span>
            <Input
              v-model:value="baseUrl"
              allow-clear
              class="lm-input"
              :placeholder="$t('settings.local_model_base_url_placeholder')"
            />
          </div>

          <div class="lm-row">
            <span class="lm-label">{{ $t('settings.local_model_model') }}</span>
            <Input
              v-model:value="model"
              allow-clear
              class="lm-input"
              :placeholder="$t('settings.local_model_model_placeholder')"
            />
          </div>

          <div class="lm-row">
            <span class="lm-label">{{ $t('settings.local_model_api_key') }}</span>
            <InputPassword
              v-model:value="apiKey"
              class="lm-input"
              autocomplete="off"
              :placeholder="hasSavedKey
                ? $t('settings.local_model_key_keep_placeholder')
                : $t('settings.local_model_key_placeholder')"
            />
            <Tag v-if="hasSavedKey" color="green">
              {{ $t('settings.local_model_key_configured') }}
            </Tag>
            <Popconfirm
              v-if="hasSavedKey"
              :title="$t('settings.local_model_key_clear_confirm')"
              :ok-text="$t('common.confirm')"
              :cancel-text="$t('common.cancel')"
              @confirm="clearKey"
            >
              <Button
                size="small"
                danger
                :loading="saving"
              >
                {{ $t('settings.local_model_key_clear') }}
              </Button>
            </Popconfirm>
          </div>

          <div class="lm-row">
            <Button
              type="primary"
              :loading="saving"
              @click="save"
            >
              {{ $t('settings.local_model_save') }}
            </Button>
            <Button
              :loading="testing"
              @click="testConnection"
            >
              {{ $t('settings.local_model_test') }}
            </Button>
            <Button
              :loading="restarting"
              @click="restartBackend"
            >
              {{ $t('settings.local_model_restart') }}
            </Button>
          </div>

          <div class="lm-hint lm-block-hint">
            {{ $t('settings.local_model_test_hint') }}
          </div>
          <div class="lm-hint">
            {{ $t('settings.local_model_key_storage_hint') }}
          </div>
        </div>
      </Spin>
    </template>
  </div>
</template>

<style scoped>
.lm-alert { margin-bottom: 12px; }
.lm-block { padding-block: 4px 12px; }
.lm-row {
  display: flex; align-items: center; gap: 10px; flex-wrap: wrap;
  padding: 6px 0;
}
.lm-label { font-size: 13px; color: var(--text-secondary); min-width: 96px; }
.lm-input { max-width: 360px; flex: 1 1 220px; }
.lm-hint { font-size: 12px; color: var(--text-tertiary); }
.lm-block-hint { margin-top: 10px; }
</style>
