<script setup lang="ts">
/**
 * 模型配置：决定 `/chat` 的模型下拉里**能选到什么**。
 *
 * 背景：`/v1/models` 只返回 `llm_models` 里 `enabled` 的行，而这些行平时靠**模型发现**
 * （按 provider 的 keyset 实时拉取 /models 写入）维护。一旦发现不成功（provider 不提供
 * /models、UA 被拦、网络不通），下拉就是空的 —— 此前前端又没有任何配置入口，
 * 于是"切不了模型"。本页提供三件事：
 *   1. 看到当前有哪些模型行（含被禁用的）；
 *   2. 启用/禁用、改上下文窗口、删除；
 *   3. **手动添加**模型（发现失败时的兜底），或点「重新发现」让后端再拉一次。
 */
import { computed, onMounted, ref } from 'vue'
import { Button, Input, InputNumber, Modal, Popconfirm, Select, Switch, Tag, message } from 'ant-design-vue'
import { DeleteOutlined, PlusOutlined, ReloadOutlined, SearchOutlined } from '@ant-design/icons-vue'
import { listModels } from '../api'
import {
  deleteAdminModel,
  listAdminModels,
  listProviderOptions,
  updateAdminModel,
  upsertAdminModel,
  type AdminModel,
  type ProviderOption,
} from '../api/modelAdmin'
import PageSkeleton from '../components/common/PageSkeleton.vue'
import EmptyState from '../components/common/EmptyState.vue'
import { useI18n } from 'vue-i18n'

const { t } = useI18n()

const loading = ref(true)
const error = ref(false)
const models = ref<AdminModel[]>([])
const providers = ref<ProviderOption[]>([])
const keyword = ref('')
const busyId = ref('')

const addOpen = ref(false)
const saving = ref(false)
const draft = ref({ provider: '', name: '', display_name: '', context_window: 128000 })

const filtered = computed(() => {
  const q = keyword.value.trim().toLowerCase()
  if (!q) return models.value
  return models.value.filter(m =>
    `${m.provider} ${m.name} ${m.display_name || ''}`.toLowerCase().includes(q))
})

const enabledCount = computed(() => models.value.filter(m => m.enabled).length)

async function load() {
  loading.value = true
  error.value = false
  try {
    models.value = await listAdminModels()
  } catch {
    error.value = true
    models.value = []
  } finally {
    loading.value = false
  }
}

async function loadProviders() {
  try {
    providers.value = await listProviderOptions()
  } catch {
    providers.value = []
  }
}

/** 「重新发现」：先打一次 /v1/models（后端按 keyset 实时拉取并写回 llm_models），再刷新列表 */
async function rediscover() {
  loading.value = true
  try {
    await listModels()
    await load()
    message.success(t('已重新拉取模型列表'))
  } catch {
    message.error(t('拉取失败，请检查该 provider 的 Key 与网络（也可手动添加模型）'))
    await load()
  } finally {
    loading.value = false
  }
}

async function toggleEnabled(m: AdminModel, value: boolean) {
  busyId.value = m.id
  try {
    await updateAdminModel(m.id, { enabled: value })
    m.enabled = value
    message.success(value ? t('已启用：会出现在对话页的模型下拉里') : t('已停用：不再出现在下拉里'))
  } catch {
    message.error(t('更新失败'))
  } finally {
    busyId.value = ''
  }
}

async function saveWindow(m: AdminModel, value: number | string | null) {
  const ctx = Number(value) || 0
  if (ctx <= 0 || ctx === m.context_window) return
  busyId.value = m.id
  try {
    await updateAdminModel(m.id, { context_window: ctx })
    m.context_window = ctx
    message.success(t('上下文窗口已更新（对话页的上下文环按它计算）'))
  } catch {
    message.error(t('更新失败'))
  } finally {
    busyId.value = ''
  }
}

async function add() {
  const { provider, name, display_name, context_window } = draft.value
  if (!provider || !name.trim()) {
    message.warning(t('请选择服务提供商并填写模型名'))
    return
  }
  saving.value = true
  try {
    await upsertAdminModel({
      provider,
      name: name.trim(),
      display_name: display_name.trim() || name.trim(),
      enabled: true,
      context_window: context_window > 0 ? context_window : 128000,
    })
    addOpen.value = false
    draft.value = { provider: '', name: '', display_name: '', context_window: 128000 }
    await load()
    message.success(t('已添加并启用'))
  } catch {
    message.error(t('添加失败'))
  } finally {
    saving.value = false
  }
}

async function remove(m: AdminModel) {
  busyId.value = m.id
  try {
    await deleteAdminModel(m.id)
    models.value = models.value.filter(x => x.id !== m.id)
    message.success(t('已删除'))
  } catch {
    message.error(t('删除失败'))
  } finally {
    busyId.value = ''
  }
}

onMounted(async () => {
  await Promise.all([load(), loadProviders()])
})
</script>

<template>
  <div class="models-view">
    <header class="mv-head">
      <div>
        <h2 class="mv-title">{{ $t('模型配置') }}</h2>
        <p class="mv-sub">
          {{ $t('只有启用（enabled）的模型才会出现在对话页的模型下拉里，共') }}
          <strong>{{ enabledCount }}</strong> {{ $t('个启用') }}
        </p>
      </div>
      <div class="mv-actions">
        <Input
          v-model:value="keyword"
          class="mv-search"
          :placeholder="$t('搜索模型名 / 提供商')"
          allow-clear
        >
          <template #prefix><SearchOutlined /></template>
        </Input>
        <Button :loading="loading" @click="rediscover">
          <template #icon><ReloadOutlined /></template>
          {{ $t('重新发现') }}
        </Button>
        <Button type="primary" @click="addOpen = true">
          <template #icon><PlusOutlined /></template>
          {{ $t('手动添加') }}
        </Button>
      </div>
    </header>

    <PageSkeleton v-if="loading && !models.length" />
    <EmptyState
      v-else-if="error"
      :title="$t('模型列表加载失败')"
      :description="$t('需要管理员权限；或后端未重启（新增的 /v1/admin/models 路由要重启才生效）')"
    />
    <EmptyState
      v-else-if="!filtered.length"
      :title="$t('还没有任何模型')"
      :description="$t('可以点「重新发现」按已配置的 Key 自动拉取，或用「手动添加」直接写入一个模型')"
    />
    <table v-else class="mv-table">
      <thead>
        <tr>
          <th>{{ $t('提供商') }}</th>
          <th>{{ $t('模型名') }}</th>
          <th>{{ $t('显示名') }}</th>
          <th class="col-ctx">{{ $t('上下文窗口') }}</th>
          <th class="col-on">{{ $t('启用') }}</th>
          <th class="col-op" />
        </tr>
      </thead>
      <tbody>
        <tr v-for="m in filtered" :key="m.id">
          <td><Tag>{{ m.provider }}</Tag></td>
          <td class="mv-name">{{ m.name }}</td>
          <td class="mv-display">{{ m.display_name || '—' }}</td>
          <td class="col-ctx">
            <InputNumber
              :value="m.context_window"
              :min="0"
              :step="1000"
              size="small"
              :disabled="busyId === m.id"
              @blur="(e: any) => saveWindow(m, e?.target?.value)"
              @press-enter="(e: any) => saveWindow(m, e?.target?.value)"
            />
          </td>
          <td class="col-on">
            <Switch
              :checked="m.enabled"
              :loading="busyId === m.id"
              size="small"
              @change="(v: any) => toggleEnabled(m, Boolean(v))"
            />
          </td>
          <td class="col-op">
            <Popconfirm
              :title="$t('删除这个模型？')"
              @confirm="remove(m)"
            >
              <Button
                type="text"
                danger
                size="small"
              >
                <template #icon><DeleteOutlined /></template>
              </Button>
            </Popconfirm>
          </td>
        </tr>
      </tbody>
    </table>

    <Modal
      v-model:open="addOpen"
      :title="$t('手动添加模型')"
      :confirm-loading="saving"
      @ok="add"
    >
      <div class="mv-form">
        <label>{{ $t('服务提供商') }}</label>
        <Select
          v-model:value="draft.provider"
          class="mv-full"
          :placeholder="$t('选择已配置 Key 的提供商')"
          :options="providers.map(p => ({ value: p.id, label: p.hasKey ? `${p.name}（已配置 Key）` : p.name }))"
        />
        <label>{{ $t('模型名（给提供商调用的 ID）') }}</label>
        <Input
          v-model:value="draft.name"
          class="mv-full"
          placeholder="deepseek-chat"
        />
        <label>{{ $t('显示名（可留空）') }}</label>
        <Input
          v-model:value="draft.display_name"
          class="mv-full"
          :placeholder="$t('下拉里显示的名字')"
        />
        <label>{{ $t('上下文窗口（tokens）') }}</label>
        <InputNumber
          v-model:value="draft.context_window"
          class="mv-full"
          :min="0"
          :step="1000"
        />
        <p class="mv-hint">
          {{ $t('模型名必须是提供商真实支持的 ID，否则对话会报模型不存在。') }}
        </p>
      </div>
    </Modal>
  </div>
</template>

<style scoped>
.models-view { padding: 16px 20px; }
.mv-head {
  display: flex; align-items: flex-start; justify-content: space-between;
  gap: 16px; flex-wrap: wrap; margin-bottom: 12px;
}
.mv-title { margin: 0; font-size: 16px; font-weight: 600; }
.mv-sub { margin: 4px 0 0; font-size: 12px; color: var(--text-tertiary); }
.mv-actions { display: flex; align-items: center; gap: 8px; }
.mv-search { width: 220px; }
.mv-table { width: 100%; border-collapse: collapse; font-size: 13px; }
.mv-table th, .mv-table td {
  padding: 8px 10px; text-align: left; border-bottom: 1px solid var(--border-subtle);
}
.mv-table th { font-weight: 500; color: var(--text-tertiary); font-size: 12px; }
.mv-name { font-family: var(--font-mono, monospace); }
.mv-display { color: var(--text-secondary); }
.col-ctx { width: 160px; }
.col-on { width: 70px; }
.col-op { width: 48px; }
.mv-form { display: flex; flex-direction: column; gap: 6px; }
.mv-form label { font-size: 12px; color: var(--text-tertiary); margin-top: 6px; }
.mv-full { width: 100%; }
.mv-hint { margin: 8px 0 0; font-size: 12px; color: var(--text-tertiary); }
</style>
